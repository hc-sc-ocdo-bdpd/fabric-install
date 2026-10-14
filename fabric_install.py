"""fabric-install: download Ubuntu .deb packages (+ dependencies), unpack them into a user
directory without root, and run them with a scoped environment."""
import hashlib, io, lzma, os, re, subprocess, tarfile, urllib.request

__all__ = ["install", "env", "run", "activate"]

MIRROR = os.environ.get("FABRIC_BINS_MIRROR", "http://archive.ubuntu.com/ubuntu")
CORE = {"libc6", "libgcc-s1", "libstdc++6"} # never bundle these: they must match the base image


# === Helpers ===
def _get(url):
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


def _fields(block):
    out = {}
    for line in block.splitlines():
        if line[:1] in (" ", "\t"): # continuation line (e.g. Description)
            continue
        k, _, v = line.partition(": ")
        out[k] = v
    return out


def _suite():
    info = dict(l.rstrip().split("=", 1) for l in open("/etc/os-release") if "=" in l)
    if info.get("ID", "").strip('"') != "ubuntu":
        raise RuntimeError(f"only Ubuntu runtimes are supported (found ID={info.get('ID')})")
    return info["VERSION_CODENAME"].strip('"')


def _index(suite):
    """Merged Packages index. Later pockets (updates, security) override the release pocket."""
    pkgs = {}
    for pocket in (suite, f"{suite}-updates", f"{suite}-security"):
        for comp in ("main", "universe"):
            raw = lzma.decompress(_get(f"{MIRROR}/dists/{pocket}/{comp}/binary-amd64/Packages.xz")).decode()
            for block in raw.split("\n\n"):
                f = _fields(block)
                if "Package" in f:
                    pkgs[f["Package"]] = f
    return pkgs


def _installed():
    """Package (and virtual package) names already on the system, read from dpkg's status file."""
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
                continue # nothing to do, or an alternative is already installed
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


def _unpack(deb, root):
    """A .deb is an ar archive; the files live in its data.tar.* member."""
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
                if hasattr(tarfile, "tar_filter"):
                    tf.extractall(root, filter=_safe) # blocks path escapes, drops setuid bits
                else:
                    tf.extractall(root)
            return
    raise ValueError("no data.tar.* in .deb")


# === Public API ===
def install(packages, root="~/.fabric_bins", resolve=True, exclude=()):
    """Install Ubuntu packages plus dependencies not already on the system into `root`.
    Returns the expanded root path."""
    root = os.path.expanduser(root)
    os.makedirs(root, exist_ok=True)
    marker = os.path.join(root, ".installed")
    done = set(open(marker).read().split()) if os.path.exists(marker) else set()
    if set(packages) <= done:
        return root
    index = _index(_suite())
    have = _installed() | set(exclude) | done
    chosen = _resolve(packages, index, have) if resolve else [index[p] for p in packages]
    for f in chosen:
        print(f"fabric-install: {f['Package']} {f['Version']}")
        deb = _get(f"{MIRROR}/{f['Filename']}")
        if hashlib.sha256(deb).hexdigest() != f["SHA256"]:
            raise RuntimeError(f"SHA256 mismatch for {f['Package']}")
        _unpack(deb, root)
        done.add(f["Package"])
    done.update(packages)
    with open(marker, "w") as fh:
        fh.write("\n".join(sorted(done)))
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