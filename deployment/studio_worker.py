"""Verify the worker SDK against the exact pinned Studio release's files."""

import os
import re
import subprocess
from pathlib import Path

import yaml

from deployment import runtime as container_runtime

ROOT = Path(os.environ["AI_ROOT"])
EXACT = re.compile(r"^[^@]+:v?\d+\.\d+\.\d+$")
WORKER_BUILD = re.compile(r"^[^@]+:sha-[0-9a-f]{40}$")

# Both runtimes hash the same ordered, compact JSON manifest, including directory
# entries so an unexpected empty directory cannot silently enter the SDK tree.
PYTHON_FINGERPRINT = r"""
import hashlib,json,sys
from pathlib import Path
root=Path(sys.argv[1])
if root.is_symlink() or not root.is_dir():
    raise SystemExit('SDK root is missing, not a directory or a symlink: '+str(root))
entries=[]
files=0
def visit(directory,excluded=False):
    global files
    for path in sorted(directory.iterdir()):
        if path.is_symlink():
            raise SystemExit('SDK symlink is forbidden: '+str(path))
        relative=path.relative_to(root).as_posix()
        if not relative.isascii():
            raise SystemExit('SDK path must be ASCII: '+relative)
        if path.is_dir():
            ignored=excluded or path.name=='__pycache__'
            if not ignored:
                entries.append({'path':relative,'type':'directory'})
            visit(path,ignored)
        elif path.is_file():
            if not excluded and not path.name.endswith('.pyc'):
                entries.append({'path':relative,'type':'file','sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
                files+=1
        else:
            raise SystemExit('Unsupported SDK entry: '+str(path))
visit(root)
if not files:
    raise SystemExit('SDK tree contains no source files: '+str(root))
entries.sort(key=lambda entry:entry['path'])
manifest=json.dumps(entries,separators=(',',':')).encode()
print(hashlib.sha256(manifest).hexdigest())
"""

NODE_FINGERPRINT = r"""
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const root=process.argv[1],entries=[];
let files=0;
const hash=value=>crypto.createHash('sha256').update(value).digest('hex');
function visit(directory,excluded=false){
  for(const name of fs.readdirSync(directory).sort()){
    const target=path.join(directory,name),stat=fs.lstatSync(target);
    if(stat.isSymbolicLink())throw new Error('SDK symlink is forbidden: '+target);
    const relative=path.relative(root,target).split(path.sep).join('/');
    if(!/^[\x00-\x7f]*$/.test(relative))throw new Error('SDK path must be ASCII: '+relative);
    if(stat.isDirectory()){
      const ignored=excluded||name==='__pycache__';
      if(!ignored)entries.push({path:relative,type:'directory'});
      visit(target,ignored);
    }else if(stat.isFile()){
      if(!excluded&&!name.endsWith('.pyc')){
        entries.push({path:relative,type:'file',sha256:hash(fs.readFileSync(target))});
        files++;
      }
    }else throw new Error('Unsupported SDK entry: '+target);
  }
}
try{
  const stat=fs.lstatSync(root);
  if(stat.isSymbolicLink()||!stat.isDirectory())throw new Error('SDK root is not a directory or is a symlink: '+root);
  visit(root);
  if(!files)throw new Error('SDK tree contains no source files: '+root);
  entries.sort((a,b)=>a.path<b.path?-1:a.path>b.path?1:0);
  console.log(hash(JSON.stringify(entries)));
}catch(error){console.error(error.message);process.exit(1);}
"""


def pinned_image(root=ROOT):
    return yaml.safe_load((root / "compose.yaml").read_text())["x-studio-worker"][
        "image"
    ]


def worker_image(root=ROOT):
    return yaml.safe_load((root / "compose.yaml").read_text())["services"][
        "comfyui-image"
    ]["image"]


def exact(image):
    return bool(EXACT.search(image))


def fingerprint(image, runtime, code, directory):
    command = container_runtime.command(
        "run",
        "--rm",
        "--network",
        "none",
        "--entrypoint",
        runtime,
        image,
        "-c" if runtime == "python3" else "-e",
        code,
        directory,
    )
    try:
        digest = subprocess.check_output(
            command, stderr=subprocess.PIPE, text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise SystemExit(
            f"Cannot fingerprint the SDK in {image} at {directory}: {detail.strip()}"
        ) from None
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise SystemExit(
            f"Invalid SDK fingerprint from {image} at {directory}; expected one SHA256 digest."
        )
    return digest


def verify(root=ROOT):
    expected = pinned_image(root)
    worker = worker_image(root)
    if not exact(expected):
        raise SystemExit(
            f"Pin {expected} to an exact release before checking the worker SDK."
        )
    if not (exact(worker) or WORKER_BUILD.fullmatch(worker)):
        raise SystemExit(
            f"Pin {worker} to an exact release or full sha-40hex build tag before checking the worker SDK."
        )
    actual = fingerprint(
        worker,
        "python3",
        PYTHON_FINGERPRINT,
        "/root/ComfyUI/custom_nodes/studio_worker",
    )
    reference = fingerprint(
        expected, "node", NODE_FINGERPRINT, "/app/comfyui/studio_worker"
    )
    if actual != reference:
        raise SystemExit(
            f"The SDK in {worker} differs from {expected}. Publish and pull a worker containing the pinned Studio SDK."
        )
    print(f"The worker SDK matches the exact Studio release {expected}.")
