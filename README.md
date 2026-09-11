# PDF 扫描裁边 (pdf-scan-crop)

Windows desktop app that trims dirty scan edges from scanned-book PDFs while keeping text, footnotes, captions, tables, artwork and normal paper margins. Optional Tesseract OCR adds a copyable text layer.

## Run

Double-click `PDF裁边.exe` (or the desktop shortcut `Apps\DIY\PDF扫描裁边.lnk`), or drop a PDF onto either. `使用说明.txt` is the full guide.

- OCR needs Tesseract at `C:\Program Files\Tesseract-OCR\tesseract.exe`; the language data ships in `tessdata\`.
- Page caches live in `%LOCALAPPDATA%\PDF裁边\cache\`, saved manual edits in `%LOCALAPPDATA%\PDF裁边\edits\`, settings in `%APPDATA%\PDF裁边\PDF裁边.ini`.

## Source and build

`source\` is a snapshot of the code this build was made from. The working copy lives in `Desktop\文件\文献处理\` (`pdf_crop_app.py` for the GUI, `fix_pdf_edges.py` for the engine and command line). To rebuild and redeploy, run from that folder in PowerShell:

    .\build_pdf_crop_exe.ps1

The script builds from `PDF裁边.spec` with Python 3.14 PyInstaller into `%TEMP%\pdf_crop_dist`, adds `tessdata\`, the guide and `source\`, then mirrors the result into this folder, leaving `.git`, `README.md` and `.gitignore` alone. The PyInstaller bundle itself (`PDF裁边.exe`, `_internal\`) is not tracked in git.
