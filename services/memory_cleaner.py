"""
Spac2 Realtime API - Maximum RAM & Garbage Collection Engine
=============================================================
Provides deep, multi-tier memory reclamation:
1. Purges expired in-memory application caches (Auth OTPs, rate-limit keys, WebRTC call signals, SQLite locks).
2. Executes cyclic Python Garbage Collection across all generations (Gen 0, 1, 2).
3. Directly instructs the OS C runtime allocator to release freed heap pages back to the Operating System:
   - Linux / Docker: glibc `malloc_trim(0)`
   - Windows: kernel32 `SetProcessWorkingSetSize(-1, -1)`
4. Adaptive Memory Watchdog that triggers emergency compaction when memory thresholds are exceeded.
"""

import os
import sys
import gc
import time
import platform
import ctypes
import logging
from typing import Dict, Any, Optional

logger = logging.getLogger("spac2.memory")

# --- OS-Level Memory Allocator Trimming Hooks ---
_libc_malloc_trim = None
_win32_empty_working_set = None

def _setup_os_trim_hooks():
    global _libc_malloc_trim, _win32_empty_working_set
    os_name = platform.system()

    if os_name == "Linux":
        # Search for libc containing malloc_trim
        for lib_candidate in ("libc.so.6", "libc.musl-x86_64.so.1", "libc.so"):
            try:
                libc = ctypes.CDLL(lib_candidate)
                if hasattr(libc, "malloc_trim"):
                    _libc_malloc_trim = libc.malloc_trim
                    _libc_malloc_trim.argtypes = [ctypes.c_size_t]
                    _libc_malloc_trim.restype = ctypes.c_int
                    logger.debug("[MemoryEngine] Configured glibc malloc_trim for Linux/Docker")
                    break
            except Exception:
                continue

    elif os_name == "Windows":
        try:
            # SetProcessWorkingSetSize with (-1, -1) empties process working set pages to standby/pagefile
            kernel32 = ctypes.windll.kernel32
            _win32_empty_working_set = kernel32.SetProcessWorkingSetSize
            _win32_empty_working_set.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_size_t]
            _win32_empty_working_set.restype = ctypes.c_int
            logger.debug("[MemoryEngine] Configured Win32 SetProcessWorkingSetSize for Windows")
        except Exception:
            pass

_setup_os_trim_hooks()


def trim_os_memory() -> bool:
    """
    Force the underlying C allocator / OS virtual memory subsystem to release unused pages
    back to the operating system immediately.
    """
    os_name = platform.system()

    if os_name == "Linux" and _libc_malloc_trim is not None:
        try:
            res = _libc_malloc_trim(0)
            return res == 1
        except Exception as e:
            logger.debug(f"[MemoryEngine] Linux malloc_trim notice: {e}")
            return False

    elif os_name == "Windows" and _win32_empty_working_set is not None:
        try:
            handle = ctypes.windll.kernel32.GetCurrentProcess()
            # Passing (SIZE_T)-1 for both min and max working set sizes empties the working set
            minus_one = ctypes.c_size_t(-1).value
            res = _win32_empty_working_set(handle, minus_one, minus_one)
            return bool(res)
        except Exception as e:
            logger.debug(f"[MemoryEngine] Windows working set trim notice: {e}")
            return False

    return False


def get_process_memory_info() -> Dict[str, Any]:
    """Inspect current process memory usage (RSS, VMS, percentage)."""
    rss = 0
    vms = 0
    percent = 0.0
    sys_percent = 0.0

    try:
        import psutil
        proc = psutil.Process()
        mem_info = proc.memory_info()
        rss = mem_info.rss
        vms = mem_info.vms
        percent = proc.memory_percent()
        sys_percent = psutil.virtual_memory().percent
    except Exception:
        # Fallback using resource module on POSIX
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * 1024
        except Exception:
            pass

    return {
        "rss_bytes": rss,
        "rss_mb": round(rss / (1024 * 1024), 2),
        "vms_mb": round(vms / (1024 * 1024), 2),
        "process_memory_percent": round(percent, 2),
        "system_memory_percent": round(sys_percent, 2),
    }


def purge_application_caches() -> Dict[str, Any]:
    """Purge stale in-memory application structures across all subsystems."""
    summary = {}

    # 1. Purge Auth OTPs and rate limiters
    try:
        from routers.auth import purge_expired_auth_caches
        summary["auth"] = purge_expired_auth_caches()
    except Exception as e:
        summary["auth_error"] = str(e)

    # 2. Prune WebSocket manager caches
    try:
        from websocket.manager import prune_manager_caches
        prune_manager_caches()
        summary["websocket"] = "pruned"
    except Exception as e:
        summary["websocket_error"] = str(e)

    # 3. Purge SQLite domain locks
    try:
        from sync.engine import purge_unused_sqlite_locks
        purged_locks = purge_unused_sqlite_locks()
        summary["sqlite_locks_purged"] = purged_locks
    except Exception as e:
        summary["sqlite_locks_error"] = str(e)

    return summary


_last_emergency_clean = 0.0

def perform_full_garbage_collection(force_trim: bool = True) -> Dict[str, Any]:
    """
    Executes maximal memory compaction:
    1. Purges all application caches and stale collections.
    2. Runs Python cyclic garbage collection for generation 2, 1, and 0.
    3. Trims OS heap pages via malloc_trim (Linux) or SetProcessWorkingSetSize (Windows).
    """
    mem_before = get_process_memory_info()
    rss_before = mem_before.get("rss_bytes", 0)

    # Step 1: Application-level in-memory cache purge
    cache_summary = purge_application_caches()

    # Step 2: Python GC across all generations
    collected_2 = gc.collect(2)
    collected_1 = gc.collect(1)
    collected_0 = gc.collect(0)
    total_objects_collected = collected_0 + collected_1 + collected_2

    # Step 3: Low-level OS Memory Reclamation
    os_trimmed = False
    if force_trim:
        os_trimmed = trim_os_memory()

    mem_after = get_process_memory_info()
    rss_after = mem_after.get("rss_bytes", 0)
    freed_bytes = max(0, rss_before - rss_after)
    freed_mb = round(freed_bytes / (1024 * 1024), 2)

    logger.info(
        f"[MAX-GC] Completed. RSS: {mem_before.get('rss_mb')} MB -> {mem_after.get('rss_mb')} MB "
        f"(Freed: {freed_mb} MB | GC Objects: {total_objects_collected} | OS Trim: {os_trimmed})"
    )

    return {
        "success": True,
        "rss_before_mb": mem_before.get("rss_mb", 0),
        "rss_after_mb": mem_after.get("rss_mb", 0),
        "freed_mb": freed_mb,
        "gc_objects_collected": total_objects_collected,
        "os_trimmed": os_trimmed,
        "platform": platform.system(),
        "cache_purges": cache_summary,
        "timestamp": int(time.time()),
    }


def adaptive_memory_watchdog(
    max_process_rss_mb: float = 350.0,
    max_system_memory_percent: float = 85.0
) -> Optional[Dict[str, Any]]:
    """
    Monitors process and system memory usage.
    If either exceeds safety limits, automatically performs emergency maximum GC.
    Debounced to prevent CPU thrashing (at least 30s interval between emergency triggers).
    """
    global _last_emergency_clean
    now = time.time()
    if now - _last_emergency_clean < 30.0:
        return None

    mem = get_process_memory_info()
    rss_mb = mem.get("rss_mb", 0)
    sys_percent = mem.get("system_memory_percent", 0)

    should_clean = False
    reason = []

    if rss_mb > max_process_rss_mb:
        should_clean = True
        reason.append(f"Process RSS ({rss_mb} MB) > threshold ({max_process_rss_mb} MB)")

    if sys_percent > max_system_memory_percent:
        should_clean = True
        reason.append(f"System RAM ({sys_percent}%) > threshold ({max_system_memory_percent}%)")

    if should_clean:
        _last_emergency_clean = now
        logger.warning(f"[MemoryWatchdog] Emergency Memory GC Triggered! Reason: {', '.join(reason)}")
        print(f"[MemoryWatchdog] Emergency Memory GC Triggered! Reason: {', '.join(reason)}")
        return perform_full_garbage_collection(force_trim=True)

    return None

