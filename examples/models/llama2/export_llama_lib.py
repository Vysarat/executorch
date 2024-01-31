# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# Example script for exporting Llama2 to flatbuffer

import argparse
import json
import logging
import shlex
from json import JSONDecodeError
from pathlib import Path
from typing import Any, Dict

import pkg_resources
import torch

from executorch.backends.xnnpack.partition.xnnpack_partitioner import XnnpackPartitioner
from executorch.exir.capture._config import EdgeCompileConfig, ExecutorchBackendConfig
from executorch.exir.passes.quant_fusion_pass import QuantFusionPass
from executorch.exir.passes.sym_shape_eval_pass import ConstraintBasedSymShapeEvalPass
from torch._export import capture_pre_autograd_graph
from torch.ao.quantization.quantize_pt2e import convert_pt2e, prepare_pt2e
from torch.ao.quantization.quantizer.embedding_quantizer import EmbeddingQuantizer

from ...portable.utils import export_to_edge, save_pte_program
from ..model_factory import EagerModelFactory
from .model import ModelArgs
from .quantize import WeightOnlyInt8QuantHandler

IS_FBCODE = True  #  os.environ.get("FBCODE_PLATFORM", False)
FORMAT = "[%(levelname)s %(asctime)s %(filename)s:%(lineno)s] %(message)s"
logging.basicConfig(level=logging.INFO, format=FORMAT)

pkg_name = __name__


def set_pkg_name(name: str) -> None:
    global pkg_name
    pkg_name = name


def get_resource_path(resource_name) -> str:
    return pkg_resources.resource_filename(pkg_name, resource_name)


def apply_embedding_bag_quantization(
    exported_model, example_inputs, so_library
) -> torch.nn.Module:
    """
    Applies embedding bag quantization on a model.
    Args:
        model: A model to apply embedding bag quantization on.
    Returns:
        An embedding bag quantized model.
    """
    try:
        _ = torch.ops.quantized_decomposed.embedding_byte.out
    except AttributeError:
        if so_library:
            print(f"Loading library {args.so_library}")
            torch.ops.load_library(args.so_library)
        else:
            raise RuntimeError(
                "Need to specify shared library path to register quantized ops (and their out variants) into EXIR.\n"
                "Follow the following steps to build the needed lib via cmake.\n"
                'Use `python -c "import torch as _; print(_.__path__)"` to find where torch package is installed.\n'
                "Set that as TORCH_PACKAGE_DIR.\n"
                "Then from root executorch dir do the following:\n"
                "rm -rf cmake-out && mkdir cmake-out && (cd cmake-out && cmake -DBUCK2=<path-to-buck2> -DCMAKE_PREFIX_PATH=$TORCH_PACKAGE_DIR -DREGISTER_QUANTIZED_OPS=ON ..) && cmake --build . -j16\n"
                'To find the location of the lib: find cmake-out -name "libquantized_ops_aot_lib*"\n'
                "Then specify the said library via -s <path to libquantized_ops_aot_lib.so\n"
            )

    embedding_quantizer = EmbeddingQuantizer()
    m = prepare_pt2e(exported_model, embedding_quantizer)
    # Calibrate
    m(*example_inputs)
    m = convert_pt2e(m)
    return m


def quantize(model) -> torch.nn.Module:
    """
    Quantizes a model by converting all weights to int8.
    Args:
        model: A model to quantize.
    Returns:
        A quantized model.
    """
    model_int8 = WeightOnlyInt8QuantHandler(model)
    model_int8_state_dict = model_int8.create_quantized_state_dict()
    model_int8 = model_int8.convert_for_runtime()
    model_int8.load_state_dict(model_int8_state_dict)
    return model_int8


def build_model(
    modelname: str = "model",
    extra_opts: str = "",
    *,
    par_local_output: bool = False,
    resource_pkg_name: str = __name__,
) -> str:
    if False:  # par_local_output:
        output_dir_path = "par:."
    else:
        output_dir_path = "."

    argString = f"--checkpoint par:{modelname}_ckpt.pt --params par:{modelname}_params.json {extra_opts} --output-dir {output_dir_path}"
    parser = build_args_parser()
    args = parser.parse_args(shlex.split(argString))
    # pkg_name = resource_pkg_name
    return export_llama(modelname, args)


def build_args_parser() -> argparse.ArgumentParser:
    ckpt_dir = f"{Path(__file__).absolute().parent.as_posix()}"
    parser = argparse.ArgumentParser()
    parser.add_argument("-o", "--output-dir", default=".", help="output directory")
    parser.add_argument(
        "-q", "--quantized_ckpt", default=None, help="quantized checkpoint file"
    )
    parser.add_argument("-Q", "--quantize", default=None, action="store_true")
    parser.add_argument(
        "-QE", "--quantize_embedding", default=None, action="store_true"
    )

    parser.add_argument(
        "-c",
        "--checkpoint",
        default=f"{ckpt_dir}/llama2.pt",
        help="checkpoint path",
    )
    parser.add_argument(
        "-kv",
        "--use_kv_cache",
        default=False,
        action="store_true",
        help="Whether or not to epxort a model using kv cache",
    )
    parser.add_argument(
        "-p", "--params", default=f"{ckpt_dir}/llama2_params.json", help="config.json"
    )
    parser.add_argument(
        "-m",
        "--metadata",
        default=None,
        help='metadata string in json format. Example {"get_bos_id": 3, "get_eos_id": 3, "get_n_bos": 1, "get_n_eos": 2}',
    )
    parser.add_argument(
        "-s",
        "--so_library",
        required=False,
        help="shared library for quantized operators",
    )

    parser.add_argument("-2", "--fairseq2", action="store_true")
    parser.add_argument("-H", "--half", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-X", "--xnnpack", action="store_true")

    return parser


def canonical_path(path: str, *, dir: bool = False) -> str:

    print(f"creating canonical path for {path}")
    if not path.startswith("par:"):
        return path

    if not IS_FBCODE:
        print("not FBCODE")
        return path[4:]
    else:
        return_val = pkg_resources.resource_filename(pkg_name, path[4:])
        print(f"canonical name is: {return_val}")
        return return_val


def get_metadata(params: ModelArgs) -> Dict[str, Any]:
    return {
        "get_vocab_size": params.vocab_size,
        "get_max_seq_len": params.max_seq_len,
        "get_n_layers": params.n_layers,
        "get_max_batch_size": params.max_batch_size,
        "get_n_kv_heads": params.n_kv_heads,
        "get_head_dim": params.dim // params.n_heads,
    }


def export_llama(modelname, args) -> str:

    checkpoint_path = canonical_path(args.checkpoint)
    params_path = canonical_path(args.params)
    output_dir_path = canonical_path(args.output_dir, dir=True)

    model, example_inputs, _ = EagerModelFactory.create_model(
        "llama2",
        "Llama2Model",
        checkpoint=checkpoint_path,
        params=params_path,
        use_kv_cache=args.use_kv_cache,
        fairseq2=args.fairseq2,
    )
    edge_config = EdgeCompileConfig(
        _check_ir_validity=False,
        _skip_type_promotion=bool(args.half),
    )

    # metadata that we want to serialize into .pte file
    metadata = get_metadata(model.params)

    if args.use_kv_cache:
        # seq length is fixed to 1 with current kv cache impl
        dynamic_shapes = None
        metadata["use_kv_cache"] = True
    else:
        dim = torch.export.Dim("token_dim", max=model.params.max_seq_len - 1)
        dynamic_shapes = {"tokens": {1: dim}}
        metadata["use_kv_cache"] = False

    if args.quantized_ckpt or args.quantize:
        modelname = f"{modelname}_q"
        model = quantize(model)

        if args.verbose:
            print(f"{modelname}:")
            print(f"{model}")

    if args.half:
        # only converts floating point dtypes to half
        # input and output are torch.long, so signature unchanged
        model.to(dtype=torch.half)
        modelname = f"{modelname}_h"
        metadata["get_dtype"] = 5
    else:
        # int8 quantization code has some bf16,
        # switch all to FP32
        model.to(dtype=torch.float)
        metadata["get_dtype"] = 6

    if args.metadata:
        try:
            extra = json.loads(args.metadata)
            for k, v in extra.items():
                metadata[k] = v
        except JSONDecodeError:
            logging.error("Invalid metadata, should be a valid JSON string")

    with torch.backends.cuda.sdp_kernel(
        enable_flash=False, enable_mem_efficient=False, enable_math=True
    ), torch.no_grad():
        m = capture_pre_autograd_graph(
            model, example_inputs, dynamic_shapes=dynamic_shapes
        )
        if args.quantize_embedding:
            m = apply_embedding_bag_quantization(m, example_inputs, args.so_library)
        edge_manager = export_to_edge(
            m,
            example_inputs,
            dynamic_shapes=dynamic_shapes,
            edge_constant_methods=metadata,
            edge_compile_config=edge_config,
        )
    if args.xnnpack:
        edge_manager = edge_manager.to_backend(XnnpackPartitioner())
        modelname = f"xnnpack_{modelname}"

    export_program = edge_manager.to_executorch(
        ExecutorchBackendConfig(
            extract_constant_segment=True,
            passes=[
                QuantFusionPass(),
            ],
            sym_shape_eval_pass=ConstraintBasedSymShapeEvalPass(),
        )
    )
    print(
        "Required memory for activation in bytes: ",
        export_program._emitter_output.program.execution_plan[0].non_const_buffer_sizes,
    )
    save_pte_program(export_program.buffer, modelname, output_dir_path)
    output_file = f"{output_dir_path}/{modelname}.pte"

    return output_file