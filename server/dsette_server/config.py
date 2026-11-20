from dataclasses import dataclass, field
from pathlib import Path
import os

def default_data_dir():
    if os.getenv("DSETTE_DATA_DIR"):
        return Path(os.environ["DSETTE_DATA_DIR"]).expanduser()
    if Path.home() == Path("/var/lib/dsette"):
        return Path.home()
    return Path("~/.local/share/dsette").expanduser()

@dataclass
class Config:
    data_dir: Path = field(default_factory=default_data_dir)
    session_seconds: int = 900
    invite_seconds: int = 600
    max_file_size: int = field(default_factory=lambda: int(os.getenv("DSETTE_MAX_FILE_SIZE", str(2 * 1024**3))))
    device_quota: int = field(default_factory=lambda: int(os.getenv("DSETTE_DEVICE_QUOTA", str(4 * 1024**3))))
    disk_reserve: int = 64 * 1024**2
    max_chunk_size: int = 4 * 1024**2
    upload_idle_seconds: int = 86400
    tls_cert: str | None = field(default_factory=lambda: os.getenv("DSETTE_TLS_CERT"))
    tls_key: str | None = field(default_factory=lambda: os.getenv("DSETTE_TLS_KEY"))
    listen_host: str = field(default_factory=lambda: os.getenv("DSETTE_HOST", "127.0.0.1"))
    listen_port: int = field(default_factory=lambda: int(os.getenv("DSETTE_PORT", "8223")))
    allowed_roots: tuple[Path, ...] = field(default_factory=lambda: tuple(Path(p).expanduser().resolve() for p in os.getenv("DSETTE_ALLOWED_ROOTS", "~/dsette-placed").split(os.pathsep)))
    @property
    def db(self): return self.data_dir / "dsette.db"

config = Config()

