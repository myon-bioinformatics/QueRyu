import json
from pathlib import Path
import subprocess
import sys
import tracemalloc
import types
from unittest.mock import patch
import cache_share


def main():
    sha='4c59cb00ca2ba898573e4af95bc4a63492aa5eb9'
    old=types.ModuleType('baseline')
    exec(subprocess.check_output(['git','show',sha+':cache_share.py'],cwd=Path(__file__).resolve().parents[1]),old.__dict__)
    rows=[{'key':str(i%100),'status':200} for i in range(100000)]
    result={}
    expected=None
    for name,module in [('baseline',old),('current',cache_share)]:
        samples=[]
        for _ in range(3):
            with patch.object(module,'_rows',return_value=rows):
                tracemalloc.start()
                got=module.query(None)
                samples.append(tracemalloc.get_traced_memory()[1])
                tracemalloc.stop()
            if expected is None: expected=got
            assert got==expected
        result[name]=samples
    print(json.dumps({'baseline_sha':sha,'python':sys.version,'rows':100000,'keys':100,'peak_bytes':result,'scope':'query temporary allocations; manifest parsing and source rows excluded'},indent=2))


if __name__=='__main__':main()
