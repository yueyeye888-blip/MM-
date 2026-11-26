"""Restore this authenticated source archive to a NEW directory; never print the key."""
import argparse
import io
import os
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    sys.exit("请先在 Python 虚拟环境中安装 cryptography。")

cli=argparse.ArgumentParser(description=__doc__)
cli.add_argument('--key',type=Path,required=True)
cli.add_argument('--output',type=Path,required=True)
options=cli.parse_args()
source=Path(__file__).resolve().with_name('workspace-source.zip.fernet')
try:
    data=Fernet(options.key.read_bytes().strip()).decrypt(source.read_bytes())
except (OSError,ValueError,InvalidToken):
    sys.exit('无法验证备份：请核对恢复密钥和加密文件。未解包。')
with zipfile.ZipFile(io.BytesIO(data)) as archive:
    items=archive.infolist()
    if len({x.filename for x in items})!=len(items) or not items:sys.exit('归档条目无效')
    for x in items:
        p=PurePosixPath(x.filename)
        if p.is_absolute() or '..' in p.parts or len(p.parts)<2 or p.parts[0]!='trade-journal' or '\\' in x.filename or ':' in x.filename or not stat.S_ISREG(x.external_attr>>16):
            sys.exit('归档包含非程序条目，未解包。')
    if archive.testzip() is not None:sys.exit('归档校验失败，未解包。')
    if options.output.exists() or options.output.is_symlink():sys.exit('输出目录已存在，请指定一个全新的目录。')
    options.output.mkdir(mode=0o700)
    for item in items:
        dest=options.output.joinpath(*PurePosixPath(item.filename).parts)
        dest.parent.mkdir(parents=True,exist_ok=True)
        with dest.open('xb') as target:target.write(archive.read(item))
        os.chmod(dest,stat.S_IMODE(item.external_attr>>16)&0o777)
print(f'已恢复 {len(items)} 个程序文件：{options.output}')
