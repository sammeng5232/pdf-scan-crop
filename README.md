# PDF 扫描裁边

Packaged Windows application for detecting and removing dirty scan edges while preserving page content, margins, footnotes, captions, tables, and normal whitespace.

## Run

Double-click `PDF裁边.exe`. No command-line setup is required.

The full PyInstaller runtime is included under `_internal/`; `tessdata/` contains the OCR language data used by the application.

## Features

- Preview automatic edge detection before generating output.
- Manual crop adjustment, rotation, zoom, pan, and per-page overrides.
- Optional OCR for one page or the whole book.
- Chinese and English interface; the selected language is stored in `ui-settings.json`.
- Reuses rendered page images when available.

This repository contains the distributable bundle. The original standalone source project was not present beside the shortcut target.
