# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

# Example script for exporting simple models to flatbuffer

import argparse

import torch

from executorch.bundled_program.config import BundledConfig
from executorch.bundled_program.core import create_bundled_program
from executorch.bundled_program.serialize import (
    serialize_from_bundled_program_to_flatbuffer,
)
from executorch.exir import ExecutorchProgramManager

from ...models import MODEL_NAME_TO_MODEL
from ...models.model_factory import EagerModelFactory
from ...portable.utils import export_to_exec_prog


def save_bundled_program(
    method_names,
    inputs,
    exec_prog,
    graph_module,
    output_path,
):
    # Here inputs is List[Tuple[Union[torch.tenor, int, float, bool]]]. Each tuple is one input test
    # set for the model. If we wish to test the model with multiple inputs then they can be
    # appended to this list. len(inputs) == number of test sets we want to run.
    #
    # If we have multiple methods in this program then we add another list of tuples to test
    # that corresponding method. Index of list of tuples will match the index of the method's name
    # in the method_names list forwarded to BundledConfig against which it will be tested.
    bundled_inputs = [inputs for _ in range(len(method_names))]

    # For each input tuple we run the graph module and put the resulting output in a list. This
    # is repeated over all the tuples present in the input list and then repeated for each method
    # name we want to test against.
    expected_outputs = [
        [[graph_module(*x)] for x in inputs] for i in range(len(method_names))
    ]

    bundled_config = BundledConfig(
        method_names=method_names,
        inputs=bundled_inputs,
        expected_outputs=expected_outputs,
    )

    bundled_program = create_bundled_program(
        exec_prog.executorch_program, bundled_config
    )
    bundled_program_buffer = serialize_from_bundled_program_to_flatbuffer(
        bundled_program
    )

    with open(output_path, "wb") as file:
        file.write(bundled_program_buffer)


def export_to_bundled_program(model_name, model, method_names, example_inputs):
    exec_prog = export_to_exec_prog(model, example_inputs)

    # Just as an example to show how multiple input sets can be bundled along, here we
    # create a list with the example_inputs tuple used twice. Each instance of example_inputs
    # is a Tuple[Union[torch.tenor, int, float, bool]] which represents one test set for the model.
    bundled_inputs = [example_inputs, example_inputs]
    print(f"Saving exported program to {model_name}_bundled.bp")
    save_bundled_program(
        method_names, bundled_inputs, exec_prog, model, f"{model_name}_bundled.bp"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-m",
        "--model_name",
        required=True,
        help=f"provide a model name. Valid ones: {list(MODEL_NAME_TO_MODEL.keys())}",
    )

    args = parser.parse_args()

    if args.model_name not in MODEL_NAME_TO_MODEL:
        raise RuntimeError(
            f"Model {args.model_name} is not a valid name. "
            f"Available models are {list(MODEL_NAME_TO_MODEL.keys())}."
        )

    model, example_inputs = EagerModelFactory.create_model(
        *MODEL_NAME_TO_MODEL[args.model_name]
    )

    method_names = ["forward"]

    export_to_bundled_program(args.model_name, model, method_names, example_inputs)
