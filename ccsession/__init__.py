"""ccsession: reads this machine's Claude Code sessions (live, transcripts, desktop index)."""
from .reader import (  # noqa: F401
    DEFAULT_CONFIG, attribute_project, build_session, clip, data_dirs, discover_data_dirs, iso_ms,
    is_live, join_live, load_config, pid_alive, project_of, read_all, read_desktop_index, read_live,
    real_start_utc, same_start, scan_index, scan_transcript, to_ms, transcript_paths,
)

__version__ = "0.1.0"
