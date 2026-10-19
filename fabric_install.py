"""fabric-install: download Ubuntu .deb packages (+ dependencies), unpack them into a user
directory without root, and run them with a scoped environment."""
import hashlib, io, lzma, os, re, subprocess, tarfile, time, urllib.request

__all__ = ["install", "env", "run", "activate"]

DEFAULT_SUITE = "jammy" # used when the host is not Ubuntu; override with install(suite=...)
MIRRORS = ([os.environ["FABRIC_INSTALL_MIRROR"]] if os.environ.get("FABRIC_INSTALL_MIRROR") else [
    "http://archive.ubuntu.com/ubuntu",
    "https://mirror.math.princeton.edu/pub/ubuntu",
    "https://azure.archive.ubuntu.com/ubuntu",
])
CORE = {"libc6", "libgcc-s1", "libstdc++6"}
_good_mirror = []


# === Helpers ===
def _get(url, deadline=None):
    """Download `url`. If `deadline` (a time.monotonic() value) is given, read the body in chunks
    and raise TimeoutError as soon as the deadline passes, even mid-download."""
    if deadline is None:
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read()
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"download time limit exceeded: {url}")
    with urllib.request.urlopen(url, timeout=min(120, remaining)) as r:
        chunks = []
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError(f"download time limit exceeded: {url}")
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)


def _fetch(path, deadline=None):
    """Download `path` from the first mirror that answers (that mirror is then tried first).
    `deadline` covers all mirrors: once it passes, TimeoutError is raised instead of trying the next one."""
    errors = []
    for m in _good_mirror + [m for m in MIRRORS if m not in _good_mirror]:
        if deadline is not None and time.monotonic() > deadline:
            raise TimeoutError(f"download time limit exceeded for {path}:\n" + "\n".join(errors))
        try:
            data = _get(f"{m}/{path}", deadline)
            _good_mirror[:] = [m]
            return data
        except Exception as e:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"download time limit exceeded for {path} (at {m}: {e})") from e
            errors.append(f"  {m}: {e}")
    raise RuntimeError("all mirrors failed:\n" + "\n".join(errors))


def _fields(block):
    out = {}
    for line in block.splitlines():
        if line[:1] in (" ", "\t"): # continuation line (e.g. Description)
            continue
        k, _, v = line.partition(": ")
        out[k] = v
    return out


def _suite(suite=None):
    if suite:
        return suite
    try:
        info = dict(l.rstrip().split("=", 1) for l in open("/etc/os-release") if "=" in l)
    except OSError:
        info = {}
    if info.get("ID", "").strip('"') == "ubuntu":
        return info["VERSION_CODENAME"].strip('"') # best compatibility on Ubuntu hosts
    return DEFAULT_SUITE


def _index(suite):
    """Merged Packages index. Later pockets (updates, security) override the release pocket."""
    pkgs = {}
    for pocket in (suite, f"{suite}-updates", f"{suite}-security"):
        for comp in ("main", "universe"):
            raw = lzma.decompress(_fetch(f"dists/{pocket}/{comp}/binary-amd64/Packages.xz")).decode()
            for block in raw.split("\n\n"):
                f = _fields(block)
                if "Package" in f:
                    pkgs[f["Package"]] = f
    return pkgs


def _installed():
    """Package (and virtual package) names already on the system, from dpkg's status file.
    Returns just CORE on hosts without dpkg (e.g. Mariner)."""
    have = set(CORE)
    try:
        text = open("/var/lib/dpkg/status").read()
    except OSError:
        return have
    for block in text.split("\n\n"):
        f = _fields(block)
        if f.get("Status", "").endswith(" installed"):
            have.add(f["Package"])
            have.update(p.strip().split(" ")[0] for p in f.get("Provides", "").split(","))
    return have


def _system_libs():
    """Sonames the dynamic linker already knows about (from `ldconfig -p`)."""
    for exe in ("ldconfig", "/sbin/ldconfig", "/usr/sbin/ldconfig"):
        try:
            out = subprocess.run([exe, "-p"], capture_output=True, text=True).stdout
            return {l.split()[0] for l in out.splitlines() if "=>" in l}
        except OSError:
            continue
    return set()


def _resolve(names, index, have):
    """Dependency closure of `names`, minus anything in `have`. Version constraints are ignored."""
    provides = {}
    for n, f in index.items():
        for p in f.get("Provides", "").split(","):
            provides.setdefault(p.strip().split(" ")[0], n)
    for n in names:
        if n not in index and n not in provides:
            raise KeyError(f"package not found in the Ubuntu index: {n}")
    todo, seen, out = list(names), set(), []
    while todo:
        n = todo.pop(0)
        n = n if n in index else provides.get(n)
        if n is None or n in seen or n in have:
            continue
        seen.add(n)
        f = index[n]
        out.append(f)
        for clause in (f.get("Pre-Depends", "") + "," + f.get("Depends", "")).split(","):
            alts = [re.split(r"[ (:]", a.strip(), maxsplit=1)[0] for a in clause.split("|") if a.strip()]
            if not alts or any(a in have for a in alts):
                continue
            todo.append(next((a for a in alts if a in index or a in provides), alts[0]))
    return out


def _unzstd(b):
    try:
        import zstandard
        return zstandard.ZstdDecompressor().stream_reader(io.BytesIO(b)).read()
    except ImportError: # Python 3.14+
        from compression import zstd
        return zstd.decompress(b)


def _safe(member, dest):
    """tarfile's 'tar' filter, but skip (with a warning) members it rejects, e.g. files that would be
    written through an absolute symlink, instead of aborting the whole install."""
    try:
        return tarfile.tar_filter(member, dest)
    except tarfile.FilterError as e:
        print(f"fabric-install: skipping {member.name}: {e}")
        return None


def _unpack(deb, root, system_libs=None):
    """A .deb is an ar archive; the files live in its data.tar.* member.
    If `system_libs` is given and the package ships shared libraries that the system already
    has (every versioned soname present), skip it. Returns True if unpacked."""
    if deb[:8] != b"!<arch>\n":
        raise ValueError("not a .deb")
    pos = 8
    while pos + 60 <= len(deb):
        name = deb[pos:pos + 16].decode().strip().rstrip("/")
        size = int(deb[pos + 48:pos + 58])
        body = deb[pos + 60:pos + 60 + size]
        pos += 60 + size + (size & 1) # members are 2-byte aligned
        if name.startswith("data.tar"):
            if name.endswith(".zst"):
                body = _unzstd(body)
            with tarfile.open(fileobj=io.BytesIO(body), mode="r:*") as tf:
                if system_libs is not None:
                    sonames = {os.path.basename(n) for n in tf.getnames()
                               if re.search(r"\.so\.\d+$", n)}
                    if sonames and sonames <= system_libs:
                        return False
                if hasattr(tarfile, "tar_filter"):
                    tf.extractall(root, filter=_safe) # blocks path escapes, drops setuid bits
                else:
                    tf.extractall(root)
            return True
    raise ValueError("no data.tar.* in .deb")


# === Public API ===
def install(packages, root="~/.fabric_install", suite=None, resolve=True, exclude=(), download_timeout=300):
    """Install Ubuntu packages (plus dependencies the system lacks) into `root`.
    suite: Ubuntu release to take packages from (default: the host's if Ubuntu, else "noble").
    download_timeout: max seconds to download each package (across all mirrors); raises
    TimeoutError if exceeded. None disables the limit. Progress is saved after each package,
    so a re-run resumes where it stopped.
    On hosts without dpkg, a dependency is skipped when the system already provides all of its
    shared libraries; packages you list yourself are always unpacked.
    Returns the expanded root path."""
    root = os.path.expanduser(root)
    os.makedirs(root, exist_ok=True)
    marker = os.path.join(root, ".installed")
    done = set(open(marker).read().split()) if os.path.exists(marker) else set()
    if set(packages) <= done:
        return root

    def save():
        with open(marker, "w") as fh:
            fh.write("\n".join(sorted(done)))

    index = _index(_suite(suite))
    have = _installed() | set(exclude) | done
    chosen = _resolve(packages, index, have) if resolve else [index[p] for p in packages]
    system_libs = None if os.path.exists("/var/lib/dpkg/status") else _system_libs()
    for f in chosen:
        deadline = None if download_timeout is None else time.monotonic() + download_timeout
        deb = _fetch(f["Filename"], deadline)
        if hashlib.sha256(deb).hexdigest() != f["SHA256"]:
            raise RuntimeError(f"SHA256 mismatch for {f['Package']}")
        listed = f["Package"] in packages
        if _unpack(deb, root, None if listed else system_libs):
            print(f"fabric-install: {f['Package']} {f['Version']}")
        else:
            print(f"fabric-install: {f['Package']} skipped (system already provides its libraries)")
        done.add(f["Package"])
        save()
    done.update(packages)
    save()
    return root


def env(root, extra=None, lib_first=True, base=None):
    """Environment dict for running binaries installed under `root`. `extra` values may use {root}.
    lib_first=True lets bundled libs shadow system ones (needed when the system lib is too old);
    False makes them only fill gaps."""
    root = os.path.expanduser(root)
    e = dict(base if base is not None else os.environ)
    libs = sorted({d for d, _, fs in os.walk(root) if any(".so" in f for f in fs)})
    old = [p for p in e.get("LD_LIBRARY_PATH", "").split(":") if p]
    e["LD_LIBRARY_PATH"] = ":".join(libs + old if lib_first else old + libs)
    bins = [os.path.join(root, p) for p in ("usr/bin", "usr/local/bin", "bin")]
    e["PATH"] = ":".join([b for b in bins if os.path.isdir(b)] + [e.get("PATH", "")])
    for k, v in (extra or {}).items():
        e[k] = v.format(root=root)
    return e


def run(cmd, root, extra=None, lib_first=True, **kwargs):
    """subprocess.run with the installed tree's environment applied to this call only."""
    return subprocess.run(cmd, env=env(root, extra, lib_first), **kwargs)


def activate(root, extra=None, lib_first=True):
    """Apply the environment to this process (affects child processes started afterwards,
    not libraries already loaded in-process)."""
    os.environ.update(env(root, extra, lib_first))
