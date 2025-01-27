import json
import importlib
import argparse
import os

from inspect import getmembers, isfunction, isclass

import fractal_tasks_core

from fractal_tasks_core.channels import ChannelInputModel
from fractal_tasks_core.tasks.cellpose_utils import CellposeChannel1InputModel
from fractal_tasks_core.tasks.cellpose_utils import CellposeChannel2InputModel
from fractal_tasks_core.tasks.cellpose_utils import CellposeCustomNormalizer
from fractal_tasks_core.tasks.cellpose_utils import CellposeModelParams

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', type=str)
    parser.add_argument('--path_to_task_args', type=str)

    args = parser.parse_args()

    with open(args.path_to_task_args) as f:
        task_args = json.load(f)

    for key in task_args.keys():
        if isinstance(task_args[key], dict):
            type_func = getattr(fractal_tasks_core.tasks.cellpose_utils, task_args[key]['type'])
            task_args[key] = type_func(**task_args[key]['args'])

    executable_name = os.path.splitext(os.path.basename(args.executable))[0]

    spec = importlib.util.spec_from_file_location(f'{executable_name}', args.executable)
    task_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(task_module)

    task_func = getattr(task_module, executable_name)
    task_func(**task_args)
