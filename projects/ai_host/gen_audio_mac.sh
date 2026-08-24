#!/bin/bash
# gen_audio_mac.sh —— 在 Mac 上批量生成语音播报 wav
#
# 用法：
#   1. 把 ai_host 目录拷到 Mac（或直接在 Mac 上 clone 仓库）
#   2. cd ai_host && ./gen_audio_mac.sh
#   3. 生成的 wav 在 audio/ 目录，文件名与 voice_manifest.json 的 key 一致
#      （如 audio/greet.wav），拷回板端 /usr/share/ai_host/audio/ 即可
#
# 依赖：macOS 自带的 say（中文语音 Tingting）、afconvert、python3
# 流程：say 先出 AIFF，再 afconvert 转 16bit 小端 PCM WAV（板端 aplay 直接放）

set -e
cd "$(dirname "$0")"

MANIFEST="voice_manifest.json"
OUT_DIR="audio"
VOICE="Tingting"

mkdir -p "$OUT_DIR"

# 用 python3 把 JSON 清单展开成「key<TAB>文案」行，逐行合成
python3 -c "
import json, sys
with open('$MANIFEST', encoding='utf-8') as f:
    for k, v in json.load(f).items():
        sys.stdout.write(k + '\t' + v + '\n')
" | while IFS=$'\t' read -r key text; do
    aiff="$OUT_DIR/$key.aiff"
    wav="$OUT_DIR/$key.wav"
    echo "生成 $wav : $text"
    say -v "$VOICE" -o "$aiff" "$text"
    afconvert -f WAVE -d LEI16 "$aiff" "$wav"
    rm -f "$aiff"
done

echo "全部完成，共 $(ls "$OUT_DIR"/*.wav | wc -l | tr -d ' ') 个 wav，位于 $OUT_DIR/"
