from pathlib import Path
import subprocess



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


def _get_cpu(lscpu) -> (int, int, int):
    """Retrieves CPU information (min, max, nominal) from the local
    machine"""
    cpu_dict = {
    k.strip(): v.strip()
    for (k, v) in (line.split(':', maxsplit=1)
                   for line in lscpu.split('\n')
                   if not line == '')
    }
    return (round(float(cpu_dict['CPU min MHz'])/100),
            round(float(cpu_dict['CPU max MHz'])/100),
            round(float(cpu_dict['CPU MHz'])/100))


