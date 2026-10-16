# fabric-install

## Overview

Installs non-Python binaries and shared libraries into Microsoft Fabric notebooks.

Downloads Ubuntu packages and their dependencies, unpacks them into a user directory, and runs them with the right library paths.

## Usage

### Functions

| Function | What it does |
|---|---|
| `fi.install(packages, root="~/.fabric_install", suite=None, resolve=True, exclude=())` | Downloads Ubuntu packages and the dependencies the system lacks, checks their SHA256, and unpacks them into `root`. Returns the path. |
| `fi.run(cmd, root, extra=None, lib_first=True, **kwargs)` | Runs a command with the installed tree on `PATH` and `LD_LIBRARY_PATH`, for that call only. Extra keyword arguments go to `subprocess.run`. |
| `fi.env(root, extra=None, lib_first=True, base=None)` | Returns the environment dict that `run` uses, without running anything. |
| `fi.activate(root, extra=None, lib_first=True)` | Applies that environment to the notebook process, so child processes started afterwards see it. Already-loaded libraries are not affected. |

### Options

- **`suite`**: the Ubuntu release to take packages from. The default is the host's release on Ubuntu, otherwise `noble`.
  - **Important:** Fabric's glibc is older, so you will most likely need to use `"jammy"`.
- **`resolve=False`**: install exactly the packages you list, with no dependencies.
- **`exclude=[...]`**: package names to skip when resolving dependencies.
- **`extra={...}`**: extra environment variables, such as `{"TESSDATA_PREFIX": "{root}/usr/share/..."}`. `{root}` is replaced with the install path.
- **`lib_first`**: when `True` (the default), installed libraries take priority over the system's. When `False`, they only fill gaps.
- **`FABRIC_INSTALL_MIRROR`**: environment variable that replaces the default Ubuntu mirror list.

## Demo (Tesseract)

Install the package:

```python
%pip install git+https://github.com/hc-sc-ocdo-bdpd/fabric-install.git
```

Install Tesseract. Fabric's glibc is older than the newest Ubuntu release, so take packages from `jammy`:

```python
import fabric_install as fi

root = fi.install(["tesseract-ocr", "tesseract-ocr-eng"], suite="jammy")
```

Run it:

```python
import os
from PIL import Image, ImageDraw, ImageFont

img = Image.new("RGB", (500, 100), "lightblue")
ImageDraw.Draw(img).text((70, 20), "HELLO FABRIC", fill="black",
                         font=ImageFont.truetype("DejaVuSans.ttf", 50))
img.save("ocr_demo.png")

tessdata = next(d for d, _, files in os.walk(root) if "eng.traineddata" in files)

result = fi.run(["tesseract", "ocr_demo.png", "stdout"], root,
                extra={"TESSDATA_PREFIX": tessdata},
                capture_output=True, text=True, check=True)
print(result.stdout)
```

`fi.run` applies the library paths to that one command only, so the notebook's own environment is left alone.

## Use cases

Anything packaged for Ubuntu that works as a command-line tool or shared library. 

Only Tesseract has been tested on Fabric so far. The rest are likely candidates, so check each one before relying on it.

### Some examples
| Package | Use | Tested? |
|---|---|---|
| `tesseract-ocr` | OCR | ✅ Oct 2026 |
| `poppler-utils` | PDF text extraction and conversion (`pdftotext`, `pdftoppm`) | ✅ Oct 2026 |
| `ghostscript` | PDF and PostScript processing | ❌ |
| `imagemagick` | Image conversion and editing | ❌ |
| `ffmpeg` | Audio and video conversion | ❌ |
| `graphviz` | Rendering diagrams (`dot`) | ✅ Oct 2026 |
| `pandoc` | Converting between document formats | ✅ Oct 2026 |
| `antiword`, `catdoc` | Reading legacy `.doc` files | ❌ |
| `libnss3`, `libatk1.0-0`, and others | Headless Chromium -> running Playwright browsers | ❌ |

**Limits:** Ubuntu packages only, and packages that need a running service or root-level setup won't work.
