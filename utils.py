from pathlib import Path
from engfmt import Quantity, quant_to_float # 1.1.0
import subprocess
import re



def _to_abs(path: Path) -> Path:
    """Make sure the path is absolute."""
    _path = Path(path)
    if not _path.is_absolute():
        # prepend the cwd
        _path = Path(Path.cwd(), _path)
    return _path


def _check_path(backup_dir: Path) -> Path:
    """Make sure the backup_dir is created somewhere."""
    backup_path = _to_abs(backup_dir)
    # make sure it exists
    backup_path.mkdir(parents=True, exist_ok=True)
    return backup_path



class CPU:
    """Small utility class that retrieves some important data from
    CPU."""
    def __init__(self, path):
        """Initialize with the path to the file containing the
        informations about CPU, got by the command lscpu.

        Args:
            path: the path to the file"""
        self.path = Path(path)
        self.cpu_min = None
        self.cpu_max = None
        self.cpu_nom = None
        self.cpu_name = None
        self.cpu_shortname = None
    
    def get_cpu(self):
        """The function retrieves and stores CPU information (min, max, nominal).
        Returns: True if the CPU data are extracted, false otherwise."""
        with self.path.open('r') as f:
            lscpu = f.read()

        # #2 check entries exist
        cpu_dict = {
            k.strip(): v.strip()
            for (k, v) in (line.split(':', maxsplit=1)
                           for line in lscpu.split('\n')
                           if not line == '')
        }
        
        consistent = ('CPU min MHz' in cpu_dict.keys() and
                 'CPU max MHz' in cpu_dict.keys() and
                 'Model name'  in cpu_dict.keys())

        if (consistent):
            self.cpu_min = round(float(cpu_dict['CPU min MHz'])/100)
            self.cpu_max = round(float(cpu_dict['CPU max MHz'])/100)
            ## parse to get 22 of: "Intel(R) Xeon(R) CPU E-2660 0 @ 2.20GHz"
            self.cpu_nom = round(quant_to_float(
                Quantity(cpu_dict['Model name'].split('@')[1]))/100000000)
            self.cpu_name = cpu_dict['Model name']
            self.cpu_shortname = re.sub('[^a-zA-Z0-9]', '', self.cpu_name)
        else:
            print("Error while loading file, entries do not match")
            raise
            

