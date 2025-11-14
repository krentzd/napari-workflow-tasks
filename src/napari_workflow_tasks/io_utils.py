# Need to create function to untar tar.gz files

import os
import tarfile
try:
    import tomllib
except:
    import pip._vendor.tomli as tomllib
from pathlib import Path
import subprocess

class PackageImporter:
    """
    Takes in a tar.gz file and unzips it into the defined root_dir
    - Only untars pyproject.toml file and .py files
    """

    def __init__(
        self,
        root_dir=None
    ):
        if root_dir is None:
            self.root_dir = Path.home()
        elif isinstance(root_dir, str):
            self.root_dir = root_dir

        self.wf_dir = os.path.join(self.root_dir, '.napari_workflow_tasks')
        os.makedirs(self.wf_dir, exist_ok=True)

        self.loaded_packages = dict()

    # Register packages into dictionary here to keep track of what already exists
    # When closed, clean up the root dir (with del)

    def open_package(
        self,
        fpath
    ):
        """
        Get fpath to tar.gz file and untar specific files
        """

        file = tarfile.open(fpath)
        file.extractall(self.wf_dir)
        file.close()

        self.package_name = '.'.join(os.path.basename(fpath).split('.')[:-2])
        toml_data = self._open_toml_file()
        self.src_path, manifest_path = self._get_paths(toml_data)

        in_pixi_env = False
        if not self._toml_has_pixi(toml_data):
            pixi_manifest_path = self._make_pixi_env()
        else:
            pixi_manifest_path = manifest_path

        self.loaded_packages[self.package_name] = {'src_path': self.src_path, 'manifest_path': manifest_path, 'pixi_manifest_path': pixi_manifest_path}



    def _open_toml_file(
        self,
    ):
        with open(os.path.join(self.wf_dir, self.package_name, 'pyproject.toml'), 'rb') as f:
            data = tomllib.load(f)

        return data

    def _toml_has_pixi(
        self,
        data
    ):
        return any(any('pixi' in s for s in x) for x in data.values())

    def _get_paths(
        self,
        data
    ):
        if self._toml_has_pixi(data):
            src_path_ = data['tool']['hatch']['build']['targets']['wheel']['packages'][0]
        else:
            src_path_ = data['tool']['poetry']['packages'][0]['include']
        src_path = os.path.join(self.wf_dir, self.package_name, src_path_)
        manifest_path = os.path.join(src_path, '__FRACTAL_MANIFEST__.json')

        return src_path, manifest_path

    def _make_pixi_env(
        self
    ):
        os.makedirs(os.path.join(self.wf_dir, 'pixi_envs'), exist_ok=True)
        # Launch subprocess to create pixi environment
        exit_code1 = subprocess.call(['pixi', 'init', self.package_name], cwd=os.path.join(self.wf_dir, 'pixi_envs'))
        exit_code2 = subprocess.call(['pixi', 'add', self.src_path.split('/')[-1].replace('_', '-')], cwd=os.path.join(self.wf_dir, 'pixi_envs', self.package_name))
        exit_code2 = subprocess.call(['pixi', 'add', 'cellpose'], cwd=os.path.join(self.wf_dir, 'pixi_envs', self.package_name))

        return os.path.join(self.wf_dir, 'pixi_envs', self.package_name, 'pixi.toml')
