# fabric-install
Install non-Python binaries and shared libraries into Microsoft Fabric notebooks.

```python
%pip install git+https://github.com/hc-sc-ocdo-bdpd/fabric-install.git

import fabric_install as fi
root = fi.install(["tesseract-ocr", "tesseract-ocr-eng"])
fi.run(["tesseract", "--version"], root)
```

Build: `python -m build --wheel`