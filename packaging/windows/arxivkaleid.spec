# -*- mode: python ; coding: utf-8 -*-
import os
from pathlib import Path

root = Path(SPECPATH).resolve().parents[1]
stage = Path(os.environ['ARXIVKALEID_BUILD_STAGE']).resolve()
if not stage.is_relative_to(root / '.desktop-build'):
    raise RuntimeError('build_stage_outside_project')
datas = [(str(p), str(p.parent.relative_to(stage))) for p in stage.rglob('*') if p.is_file()]
a = Analysis(
    [str(root / 'packaging/windows/entry.py')],
    pathex=[str(root), str(root / 'packaging/windows')],
    binaries=[], datas=datas, hiddenimports=['pypdf', 'qfluentwidgets', 'qframelesswindow'],
    excludes=['tkinter', 'unittest', 'pytest', 'setuptools', 'pip', 'pkg_resources', 'PySide6.QtQml', 'PySide6.QtQuick',
              'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets'],
    noarchive=False,
)
# Fluent 的内嵌 SVG 图标需要 Svg/SvgWidgets/Xml；仍不带 PDF、视频、WebEngine。
plugin_names = {'qwindows.dll', 'qmodernwindowsstyle.dll', 'qsvgicon.dll'}
a.binaries = [item for item in a.binaries
              if '/plugins/' not in item[0].replace('\\', '/')
              or Path(item[0]).name in plugin_names]
a.binaries = [item for item in a.binaries
              if (not Path(item[0]).name.startswith('Qt6')
                  or Path(item[0]).name in {'Qt6Core.dll', 'Qt6Gui.dll', 'Qt6Widgets.dll',
                                           'Qt6Svg.dll', 'Qt6SvgWidgets.dll', 'Qt6Xml.dll'})
              and Path(item[0]).name.lower() not in {'opengl32sw.dll', 'd3dcompiler_47.dll'}]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='arXivKaleid',
          debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
          console=False, disable_windowed_traceback=True, contents_directory='_internal')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='arXivKaleid')
