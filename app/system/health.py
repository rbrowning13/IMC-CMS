"""
System health metrics.

All functions in this module must be:
- Read-only
- Deterministic
- Fast
- Safe to call in a request context

No shell commands. No subprocess. No network calls.
"""

from typing import Dict, Optional
import os
import shutil
import time


def disk_usage(path: str = "/") -> Dict[str, float]:
    """
    Return disk usage stats (GB) for the given path.
    """
    total, used, free = shutil.disk_usage(path)
    gb = 1024 ** 3
    return {
        "total_gb": round(total / gb, 2),
        "used_gb": round(used / gb, 2),
        "free_gb": round(free / gb, 2),
    }


def disk_health(path: str = "/") -> Dict[str, float]:
    """
    Disk health with derived percentages.
    """
    d = disk_usage(path)
    total = d.get("total_gb", 0)
    free = d.get("free_gb", 0)

    percent_free = (free / total * 100) if total else 0.0
    d["percent_free"] = round(percent_free, 1)
    return d


def disk_writable(path: str = "/") -> Optional[bool]:
    """
    Best-effort check if the disk is writable.
    """
    try:
        test_path = os.path.join(path, ".clarity_write_test")
        with open(test_path, "w") as f:
            f.write("ok")
        os.remove(test_path)
        return True
    except Exception:
        return False


def memory_usage() -> Optional[Dict[str, float]]:
    """
    Best-effort memory usage.
    Returns None if not supported on the platform.
    """
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        # ru_maxrss is KB on Linux, bytes on macOS; normalize to MB conservatively
        mem_mb = usage.ru_maxrss / 1024
        return {"process_mb": round(mem_mb, 2)}
    except Exception:
        return None


def database_size(db_path: Optional[str] = None) -> Optional[Dict[str, float]]:
    """
    Return database file size if using a local file-backed DB.
    """
    if not db_path:
        return None
    try:
        size = os.path.getsize(db_path)
        return {
            "db_size_mb": round(size / (1024 * 1024), 2)
        }
    except Exception:
        return None


# --- Temperature helpers ---
def cpu_temperature() -> Optional[float]:
    """
    Best-effort CPU temperature in Celsius.

    Linux: tries /sys/class/thermal
    macOS: not reliably available without IOKit (returns None)
    """
    # Linux thermal zones
    try:
        base = "/sys/class/thermal"
        if os.path.isdir(base):
            temps = []
            for name in os.listdir(base):
                if not name.startswith("thermal_zone"):
                    continue
                path = os.path.join(base, name, "temp")
                try:
                    with open(path, "r") as f:
                        raw = f.read().strip()
                        val = float(raw)
                        # Most Linux systems report millidegrees C
                        if val > 1000:
                            val = val / 1000.0
                        temps.append(val)
                except Exception:
                    continue
            if temps:
                return round(sum(temps) / len(temps), 1)
    except Exception:
        pass

    return None


def drive_temperatures() -> Optional[list]:
    """
    Best-effort drive temperature probe.

    Linux only, via /sys/block/*/device/hwmon
    Returns list of {name, temp_c}
    """
    try:
        results = []
        base = "/sys/block"
        if not os.path.isdir(base):
            return None

        for dev in os.listdir(base):
            hwmon_base = os.path.join(base, dev, "device", "hwmon")
            if not os.path.isdir(hwmon_base):
                continue

            for hw in os.listdir(hwmon_base):
                temp_path = os.path.join(hwmon_base, hw, "temp1_input")
                try:
                    with open(temp_path, "r") as f:
                        raw = f.read().strip()
                        val = float(raw)
                        if val > 1000:
                            val = val / 1000.0
                        results.append({
                            "device": dev,
                            "temp_c": round(val, 1),
                        })
                except Exception:
                    continue

        return results or None
    except Exception:
        return None


def uptime(start_time: float) -> Dict[str, float]:
    """
    Return uptime in seconds since start_time.
    """
    seconds = max(0.0, time.time() - start_time)
    return {
        "uptime_seconds": round(seconds, 1),
        "uptime_hours": round(seconds / 3600, 2),
    }


def basic_health_snapshot(
    *,
    data_path: str = "/",
    app_start_time: Optional[float] = None,
    backup_path: Optional[str] = None,
) -> Dict[str, object]:
    """
    High-level system health snapshot.
    """
    snapshot = {
        "disk": disk_health(data_path),
        "disk_writable": disk_writable(data_path),
        "memory": memory_usage(),
        "temps": {
            "cpu_c": cpu_temperature(),
            "drives": drive_temperatures(),
        },
        "warnings": [],
    }

    if app_start_time is not None:
        snapshot["uptime"] = uptime(app_start_time)

    if "backup_path" in locals() and backup_path:
        snapshot["backup"] = backup_status(backup_path)

    # Remove empty temp containers
    temps = snapshot.get("temps", {})
    if not temps.get("cpu_c") and not temps.get("drives"):
        snapshot.pop("temps", None)

    # ----------------------------
    # Derived warnings
    # ----------------------------
    disk = snapshot.get("disk", {})
    if disk.get("percent_free", 100) < 15:
        snapshot["warnings"].append("Low disk space")

    if snapshot.get("disk_writable") is False:
        snapshot["warnings"].append("Disk not writable")

    backup = snapshot.get("backup")
    if backup and backup.get("exists") and backup.get("age_hours", 0) > 48:
        snapshot["warnings"].append("Backup may be stale")

    if not snapshot["warnings"]:
        snapshot.pop("warnings", None)

    return snapshot


def backup_status(backup_path: Optional[str] = None) -> Optional[Dict[str, object]]:
    """
    Best-effort backup presence check.

    This does NOT verify backup correctness.
    It only answers:
    - does the path exist
    - is it readable
    - when was it last modified

    Returns None if no path is provided.
    """
    if not backup_path:
        return None

    try:
        stat = os.stat(backup_path)
        age_seconds = max(0, time.time() - stat.st_mtime)
        return {
            "path": backup_path,
            "exists": True,
            "last_modified": round(stat.st_mtime),
            "age_hours": round(age_seconds / 3600, 2),
        }
    except FileNotFoundError:
        return {
            "path": backup_path,
            "exists": False,
        }
    except Exception:
        return None
