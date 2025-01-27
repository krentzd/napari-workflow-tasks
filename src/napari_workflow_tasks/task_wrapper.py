import json
import importlib
import argparse
import os
from fractal_tasks_core.channels import ChannelInputModel

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', type=str)
    parser.add_argument('--path_to_task_args', type=str)

    args = parser.parse_args()

    with open(args.path_to_task_args) as f:
        task_args = json.load(f)

    if 'channel' in task_args.keys():
        task_args['channel'] = ChannelInputModel(label=task_args['channel'])

    executable_name = os.path.splitext(os.path.basename(args.executable))[0]

    spec = importlib.util.spec_from_file_location(f'{executable_name}', args.executable)
    task_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(task_module)

    task_func = getattr(task_module, executable_name)
    task_func(**task_args)
