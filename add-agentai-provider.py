"""把 agentai 中转站接进 DSH 的模型设置（通用 OpenAI 兼容 provider）。

做两件事：
  1) `profiles/desktop/cordis.patch.yml` 的 `llm-pi-ai.providers` 里加一条 `agentai`
     （baseURL + apiKeyEnv + 模型目录）—— 这就是「通用接口」：
     以后换任何 OpenAI 兼容中转站，只要照抄这一块改 baseURL / 模型 id 即可。
  2) `.credentials.yaml` 里加 `refs.AGENTAI_API_KEY` 与 `records.llm-pi-ai/agentai`，
     让上面的 apiKeyEnv 能解析到 key（不把 key 写进 patch）。
"""
import os
import shutil
import sys
import time

import yaml

DSH = os.path.join(os.environ['LOCALAPPDATA'], 'VideoForge', 'dsh')
PROFILE = os.path.join(DSH, 'profiles', 'desktop', 'cordis.patch.yml')
CREDS = os.path.join(DSH, '.credentials.yaml')
KEY_FILE = r'D:\Tools\mcp-servers\agentai.key'
SEED_DIR = r'D:\VideoForge-dsh\deepseek-harness-master\apps\desktop\resources\videoforge-seed'

key = open(KEY_FILE, encoding='utf-8').read().strip()
if not key:
    print('agentai key 为空，退出')
    sys.exit(2)

AGENTAI_PROVIDER = {
    'displayName': 'agentai 中转站（OpenAI 兼容）',
    'api': 'openai-completions',
    'baseURL': 'https://agentai.token6688.com/v1',
    'apiKeyEnv': 'AGENTAI_API_KEY',
    'defaultInput': ['text', 'image'],
    'defaultContextWindow': 262144,
    'defaultMaxTokens': 32768,
    'models': [
        {'id': 'claude-sonnet-5', 'name': 'Claude Sonnet 5', 'input': ['text', 'image']},
        {'id': 'claude-opus-5', 'name': 'Claude Opus 5', 'input': ['text', 'image']},
        {'id': 'gpt-5.5', 'name': 'GPT-5.5', 'input': ['text', 'image']},
        {'id': 'gemini-3-pro', 'name': 'Gemini 3 Pro', 'input': ['text', 'image']},
        {'id': 'deepseek-v4-pro', 'name': 'DeepSeek V4 Pro', 'input': ['text']},
        {'id': 'deepseek-v4.1-flash', 'name': 'DeepSeek V4.1 Flash', 'input': ['text']},
        {'id': 'qwen3.8-max', 'name': 'Qwen3.8 Max', 'input': ['text', 'image']},
        {'id': 'glm-5.3', 'name': 'GLM-5.3', 'input': ['text']},
        {'id': 'kimi-k3', 'name': 'Kimi K3', 'input': ['text']},
        {'id': 'minimax-m3', 'name': 'MiniMax M3', 'input': ['text', 'image']},
        {'id': 'doubao-seed-2-0-pro', 'name': 'Doubao Seed 2.0 Pro', 'input': ['text', 'image']},
    ],
}


def patch_profile() -> None:
    text = open(PROFILE, encoding='utf-8').read()
    doc = yaml.safe_load(text)
    entry = next((e for e in doc if isinstance(e, dict) and e.get('id') == 'llm-pi-ai'), None)
    if entry is None:
        raise SystemExit('profile patch 里没有 llm-pi-ai 条目')
    providers = entry.setdefault('config', {}).setdefault('providers', {})
    providers['agentai'] = AGENTAI_PROVIDER
    shutil.copyfile(PROFILE, PROFILE + '.bak-agentai-' + time.strftime('%Y%m%d-%H%M%S'))
    with open(PROFILE, 'w', encoding='utf-8', newline='\n') as fh:
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    # 自证：写完还能解析，且 agentai 在
    again = yaml.safe_load(open(PROFILE, encoding='utf-8'))
    ok = any(e.get('id') == 'llm-pi-ai' and 'agentai' in (e.get('config', {}).get('providers') or {})
             for e in again if isinstance(e, dict))
    print('profile patch 写入 agentai:', ok)


def patch_credentials() -> None:
    text = open(CREDS, encoding='utf-8').read()
    doc = yaml.safe_load(text) or {}
    refs = doc.setdefault('refs', {})
    refs['AGENTAI_API_KEY'] = key
    records = doc.setdefault('records', {})
    records['llm-pi-ai/agentai'] = {'kind': 'api-key', 'key': key}
    shutil.copyfile(CREDS, CREDS + '.bak-agentai-' + time.strftime('%Y%m%d-%H%M%S'))
    with open(CREDS, 'w', encoding='utf-8', newline='\n') as fh:
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    again = yaml.safe_load(open(CREDS, encoding='utf-8'))
    print('credentials 写入 AGENTAI_API_KEY:', 'AGENTAI_API_KEY' in (again.get('refs') or {}))


def patch_seed() -> None:
    """把同样的配置写进随包种子（新机器装完就有 agentai 可选）。"""
    seed_settings = os.path.join(SEED_DIR, 'settings.yaml')
    seed_creds = os.path.join(SEED_DIR, 'credentials.yaml')
    doc = yaml.safe_load(open(seed_settings, encoding='utf-8')) or {}
    doc.setdefault('llm-pi-ai', {}).setdefault('providers', {})['agentai'] = AGENTAI_PROVIDER
    with open(seed_settings, 'w', encoding='utf-8', newline='\n') as fh:
        yaml.safe_dump(doc, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
    print('seed settings 写入 agentai')
    if os.path.exists(seed_creds):
        cdoc = yaml.safe_load(open(seed_creds, encoding='utf-8')) or {}
        cdoc.setdefault('refs', {})['AGENTAI_API_KEY'] = key
        cdoc.setdefault('records', {})['llm-pi-ai/agentai'] = {'kind': 'api-key', 'key': key}
        with open(seed_creds, 'w', encoding='utf-8', newline='\n') as fh:
            yaml.safe_dump(cdoc, fh, allow_unicode=True, sort_keys=False, default_flow_style=False)
        print('seed credentials 写入 AGENTAI_API_KEY')


patch_profile()
patch_credentials()
patch_seed()
