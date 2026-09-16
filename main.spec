# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files
import certifi
block_cipher = None

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('templates/*.html', 'templates'),
        ('templates/admin/*.html', 'templates/admin'),
        ('static/images', 'static/images'),
        ('static/css', 'static/css'),
        (certifi.where(), 'certifi')
    ] + collect_data_files('psycopg2', include_py_files=True),
    hiddenimports=[
        'json',
        'psycopg2',
        'psycopg2.extras',
        'dns.resolver'
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='main',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    name='main'
)
