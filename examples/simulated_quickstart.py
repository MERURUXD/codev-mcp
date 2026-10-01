"""Reproducible stdio MCP demonstration; all numbers are simulated."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from codev_mcp.stdio_client import StdioClient


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    request = json.loads(Path(__file__).with_name('create-singlet.json').read_text(encoding='utf-8'))
    client = StdioClient(root / 'work', 'simulated', 60, root / 'protocol')
    try:
        tools = client.request('tools/list', {})['tools']
        lens = client.call('create_lens', {'request': request})[0]
        client.call('run_analysis', {'request': {'kind': 'first_order'}})
        analysis = client.call('get_analysis', {})[0]
        for _ in range(4):
            if analysis['task']['state'] not in {'queued', 'running'}:
                break
            analysis = client.call('get_analysis', {})[0]
        saved = client.call('save_lens_as', {'path': str(root / 'simulated.len')})[0]
        status = client.call('get_status', {})[0]
        client.call('close_session', {})
        result = {'source': 'simulated', 'tool_count': len(tools), 'lens': lens,
                  'analysis': analysis, 'saved': saved, 'status': status}
        (root / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8', newline='\n')
        assert len(tools) == 11 and analysis['source'] == 'simulated'
        assert analysis['task']['state'] == 'succeeded'
    finally:
        client.close()
    print(root / 'result.json')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
