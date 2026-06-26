# Cloud GPU vs Local GPU Desktop Decision

Audience: solo Windows developer using Python/Streamlit/FRED/rules-engine work plus VBA/VB.NET tooling.

Workloads:

- Batch transcription with `whisper.cpp` plus `pyannote` on call audio.
- 7B-13B LLM experimentation/fine-tuning.
- General AI development.

Decision context:

- Usage is bursty/project-based, not constant.
- Some mortgage borrower call audio is PII-adjacent.
- Data residency matters for sensitive call-audio only.

Recommendation:

```text
Choose Path B now:
  $600 x86 Ryzen laptop as thin client
  + rented RunPod Secure Cloud GPU on demand

Buy a local GPU desktop only if:
  sensitive transcription volume is high,
  turnaround needs are constant,
  or RTX 4090-equivalent usage is consistently >150 GPU-hours/month.
```

## Clarifying questions

Ask before final implementation:

1. Sensitive call-audio volume: `<10`, `10-100`, or `100+` audio-hours/month?
2. Is any third-party US cloud acceptable for mortgage audio, or strict local only?
3. Fine-tuning target: QLoRA/LoRA experiments, or full fine-tune?
4. Need 13B with long context, e.g. 16k-32k tokens?
5. OK using WSL2/Linux commands from Windows?

Assumption used below:

```text
Sensitive audio stays local.
Cloud is for non-sensitive audio and model experimentation.
Fine-tuning means LoRA/QLoRA, not full 13B training.
```

## 1. Break-even table

Assumptions:

```text
Desktop GPU box:        $2,500 / $3,250 / $4,000
Depreciation:           36 months
Local electricity:      0.8 kW * $0.18/kWh = $0.14/GPU-hour
Local storage:          2TB NVMe amortized ~= $6/month
RunPod network volume:  500GB = $35/month, 1TB = $70/month
RunPod egress:          $0
Idle risk:              +10% compute waste

RunPod rates:
  RTX 4090: $0.34/hr -> $0.374/hr with idle risk
  A100:     $1.39/hr -> $1.529/hr with idle risk
  H100:     $2.89/hr -> $3.179/hr with idle risk
```

Break-even monthly cloud GPU-hours where owning starts winning:

| Owned desktop cost | Cloud GPU | 500GB cloud volume | 1TB cloud volume |
|---:|---:|---:|---:|
| $2,500 | RTX 4090 | ~171 hrs/month | ~22 hrs/month |
| $3,250 | RTX 4090 | ~260 hrs/month | ~111 hrs/month |
| $4,000 | RTX 4090 | ~349 hrs/month | ~200 hrs/month |
| $2,500 | A100 | ~29 hrs/month | ~4 hrs/month |
| $3,250 | A100 | ~44 hrs/month | ~19 hrs/month |
| $4,000 | A100 | ~59 hrs/month | ~34 hrs/month |
| $2,500 | H100 | ~13 hrs/month | ~2 hrs/month |
| $3,250 | H100 | ~20 hrs/month | ~9 hrs/month |
| $4,000 | H100 | ~27 hrs/month | ~15 hrs/month |

Practical decision:

```text
<50 cloud GPU-hours/month, bursty:
  Path B.

50-150 cloud GPU-hours/month:
  Path B unless sensitive local transcription is frequent.

150+ RTX 4090-equivalent GPU-hours/month:
  Consider owning.

High sensitive-audio volume plus fast turnaround:
  Own/local GPU is cleaner.
```

Important caveat:

```text
A100/H100 hours are not equivalent to RTX 4090 hours.
They have more VRAM and may finish some training/fine-tuning jobs faster.
```

## 2. Path B architecture

Provider:

```text
Primary:
  RunPod Secure Cloud

Why:
  low hourly GPU rates
  per-second billing
  SSH access
  Jupyter support
  persistent network volumes
  no ingress/egress fees

Avoid for PII:
  Vast.ai marketplace hosts

Use instead if desired:
  Lambda for simpler A100/H100 availability and more enterprise-style UX,
  but usually at higher cost.
```

GPU choice:

```text
Whisper.cpp + pyannote, non-sensitive:
  RTX 4090 24GB
  Cost target: ~$0.34/hr
  Good for batch transcription and diarization.

13B experimentation / QLoRA:
  A100 80GB preferred
  Cost target: ~$1.39/hr
  Good for 13B QLoRA, larger batch sizes, longer context.

H100:
  Cost target: ~$2.89/hr
  Use only when wall-clock time matters or for larger training runs.
```

Persistent storage:

```text
RunPod network volume:
  500GB minimum
  1TB if storing datasets/checkpoints

Mount path:
  /workspace

Recommended layout:
  /workspace/models
  /workspace/hf
  /workspace/datasets
  /workspace/audio/in
  /workspace/audio/out
  /workspace/repos
  /workspace/venvs
```

Windows connection:

```text
Primary:
  Cursor or VS Code Remote-SSH

Secondary:
  raw ssh

Optional:
  Jupyter on port 8888
```

## 3. Runnable setup

### 3a. Windows prerequisites

PowerShell:

```powershell
winget install Microsoft.VisualStudioCode
winget install Git.Git
winget install OpenJS.NodeJS.LTS
winget install Python.Python.3.11
```

Create SSH key:

```powershell
mkdir $env:USERPROFILE\.ssh -Force
ssh-keygen -t ed25519 -f $env:USERPROFILE\.ssh\runpod_ed25519 -C "runpod-ai"
$PUBKEY = Get-Content $env:USERPROFILE\.ssh\runpod_ed25519.pub
```

Set RunPod API key for current PowerShell:

```powershell
$env:RUNPOD_API_KEY="PASTE_RUNPOD_API_KEY_HERE"
```

Save permanently:

```powershell
setx RUNPOD_API_KEY "PASTE_RUNPOD_API_KEY_HERE"
```

### 3b. Create persistent RunPod network volume

Pick a RunPod Secure Cloud data center in the RunPod UI.

Example:

```powershell
$DC="US-KS-2"
```

Create 500GB network volume:

```powershell
$volumeBody = @{
  name = "ai-work-500gb"
  size = 500
  dataCenterId = $DC
} | ConvertTo-Json

$VOL = Invoke-RestMethod `
  -Method Post `
  -Uri "https://rest.runpod.io/v1/networkvolumes" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY"; "Content-Type" = "application/json" } `
  -Body $volumeBody

$env:RUNPOD_VOLUME_ID = $VOL.id
$env:RUNPOD_VOLUME_ID
```

### 3c. Launch RTX 4090 transcription pod

```powershell
$GPU="NVIDIA GeForce RTX 4090"
$IMAGE="runpod/pytorch:2.6.0-py3.11-cuda12.4.1-devel-ubuntu22.04"
$JUPYTER_PASSWORD="change-me-long-password"
$PUBKEY = Get-Content $env:USERPROFILE\.ssh\runpod_ed25519.pub

$query = @"
mutation {
  podFindAndDeployOnDemand(
    input: {
      cloudType: SECURE
      gpuCount: 1
      gpuTypeId: "$GPU"
      name: "whisper-pyannote-4090"
      imageName: "$IMAGE"
      containerDiskInGb: 80
      minVcpuCount: 8
      minMemoryInGb: 32
      ports: "22/tcp,8888/http"
      volumeMountPath: "/workspace"
      networkVolumeId: "$env:RUNPOD_VOLUME_ID"
      env: [
        { key: "PUBLIC_KEY", value: "$PUBKEY" },
        { key: "JUPYTER_PASSWORD", value: "$JUPYTER_PASSWORD" }
      ]
    }
  ) {
    id
    imageName
    machineId
  }
}
"@

$resp = Invoke-RestMethod `
  -Method Post `
  -Uri "https://api.runpod.io/graphql?api_key=$env:RUNPOD_API_KEY" `
  -ContentType "application/json" `
  -Body (@{ query = $query } | ConvertTo-Json -Depth 10)

$env:RUNPOD_POD_ID = $resp.data.podFindAndDeployOnDemand.id
$env:RUNPOD_POD_ID
```

For A100 fine-tune pod:

```powershell
$GPU="NVIDIA A100 80GB PCIe"
```

For H100:

```powershell
$GPU="NVIDIA H100 PCIe"
```

### 3d. Get SSH host/port

Wait 1-2 minutes, then:

```powershell
$query = @"
query {
  pod(input: { podId: "$env:RUNPOD_POD_ID" }) {
    id
    desiredStatus
    runtime {
      ports {
        ip
        isIpPublic
        privatePort
        publicPort
        type
      }
    }
  }
}
"@

$pod = Invoke-RestMethod `
  -Method Post `
  -Uri "https://api.runpod.io/graphql?api_key=$env:RUNPOD_API_KEY" `
  -ContentType "application/json" `
  -Body (@{ query = $query } | ConvertTo-Json -Depth 10)

$sshPortObj = $pod.data.pod.runtime.ports | Where-Object { $_.privatePort -eq 22 }
$env:RUNPOD_SSH_HOST = $sshPortObj.ip
$env:RUNPOD_SSH_PORT = $sshPortObj.publicPort

"$env:RUNPOD_SSH_HOST $env:RUNPOD_SSH_PORT"
```

SSH:

```powershell
ssh -i $env:USERPROFILE\.ssh\runpod_ed25519 root@$env:RUNPOD_SSH_HOST -p $env:RUNPOD_SSH_PORT
```

### 3e. Configure Cursor / VS Code Remote-SSH

Append SSH config:

```powershell
@"

Host runpod-ai
  HostName $env:RUNPOD_SSH_HOST
  Port $env:RUNPOD_SSH_PORT
  User root
  IdentityFile $env:USERPROFILE\.ssh\runpod_ed25519
  StrictHostKeyChecking accept-new

"@ | Add-Content $env:USERPROFILE\.ssh\config
```

Connect with VS Code:

```powershell
code --remote ssh-remote+runpod-ai /workspace
```

Connect with Cursor:

```powershell
cursor --remote ssh-remote+runpod-ai /workspace
```

Or in Cursor:

```text
Ctrl+Shift+P
Remote-SSH: Connect to Host
runpod-ai
Open Folder: /workspace
```

### 3f. Install whisper.cpp + pyannote on pod

SSH into pod, then:

```bash
mkdir -p /workspace/{models,hf,datasets,audio/in,audio/out,repos,venvs,scripts}
export HF_HOME=/workspace/hf
export TRANSFORMERS_CACHE=/workspace/hf
export HF_HUB_CACHE=/workspace/hf/hub

apt-get update
apt-get install -y git cmake build-essential ffmpeg python3-venv python3-pip rsync

cd /workspace/repos
git clone https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
cmake -B build -DGGML_CUDA=ON
cmake --build build -j"$(nproc)"

mkdir -p /workspace/models/whisper
bash ./models/download-ggml-model.sh large-v3-turbo
mv models/ggml-large-v3-turbo.bin /workspace/models/whisper/
```

Python env:

```bash
python3 -m venv /workspace/venvs/audio
source /workspace/venvs/audio/bin/activate
pip install --upgrade pip wheel setuptools
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu124
pip install pyannote.audio soundfile librosa tqdm
```

Set Hugging Face token:

```bash
export HF_TOKEN="PASTE_HUGGINGFACE_TOKEN_HERE"
```

Accept model terms in Hugging Face for:

```text
pyannote/speaker-diarization-3.1
pyannote/segmentation-3.0
```

### 3g. Upload non-sensitive audio folder

PowerShell with `rsync`:

```powershell
rsync -avP `
  -e "ssh -i $env:USERPROFILE\.ssh\runpod_ed25519 -p $env:RUNPOD_SSH_PORT" `
  "C:\path\to\non_sensitive_audio\" `
  "root@$env:RUNPOD_SSH_HOST:/workspace/audio/in/"
```

PowerShell with `scp`:

```powershell
scp -i $env:USERPROFILE\.ssh\runpod_ed25519 -P $env:RUNPOD_SSH_PORT -r `
  "C:\path\to\non_sensitive_audio\*" `
  root@$env:RUNPOD_SSH_HOST:/workspace/audio/in/
```

### 3h. Batch whisper.cpp + pyannote script

On pod:

```bash
cat > /workspace/scripts/batch_transcribe_diarize.sh <<'BASH'
#!/usr/bin/env bash
set -euo pipefail

IN_DIR="${1:-/workspace/audio/in}"
OUT_DIR="${2:-/workspace/audio/out}"
WORK_DIR="/workspace/audio/work"
WHISPER_CPP="/workspace/repos/whisper.cpp"
WHISPER_MODEL="/workspace/models/whisper/ggml-large-v3-turbo.bin"

mkdir -p "$OUT_DIR" "$WORK_DIR"

find "$IN_DIR" -type f \( -iname "*.wav" -o -iname "*.mp3" -o -iname "*.m4a" -o -iname "*.flac" -o -iname "*.ogg" \) | while read -r src; do
  base="$(basename "${src%.*}")"
  wav="$WORK_DIR/${base}.16k.wav"
  outbase="$OUT_DIR/${base}"

  echo "==> Converting: $src"
  ffmpeg -y -hide_banner -loglevel error -i "$src" -ar 16000 -ac 1 -c:a pcm_s16le "$wav"

  echo "==> Whisper: $base"
  "$WHISPER_CPP/build/bin/whisper-cli" \
    -m "$WHISPER_MODEL" \
    -f "$wav" \
    -l en \
    -oj \
    -osrt \
    -of "$outbase"
done

source /workspace/venvs/audio/bin/activate
python /workspace/scripts/diarize_folder.py "$WORK_DIR" "$OUT_DIR"
BASH

chmod +x /workspace/scripts/batch_transcribe_diarize.sh
```

Create diarization script:

```bash
cat > /workspace/scripts/diarize_folder.py <<'PY'
import os
import sys
from pathlib import Path

import torch
from pyannote.audio import Pipeline

in_dir = Path(sys.argv[1])
out_dir = Path(sys.argv[2])
hf_token = os.environ.get("HF_TOKEN")

if not hf_token:
    raise SystemExit("Set HF_TOKEN before running diarization.")

pipeline = Pipeline.from_pretrained(
    "pyannote/speaker-diarization-3.1",
    use_auth_token=hf_token,
)

if torch.cuda.is_available():
    pipeline.to(torch.device("cuda"))

for wav in sorted(in_dir.glob("*.16k.wav")):
    print(f"==> Diarize: {wav.name}", flush=True)
    diarization = pipeline(str(wav))

    rttm_path = out_dir / f"{wav.stem}.rttm"
    txt_path = out_dir / f"{wav.stem}.speakers.txt"

    with rttm_path.open("w", encoding="utf-8") as f:
        diarization.write_rttm(f)

    with txt_path.open("w", encoding="utf-8") as f:
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            f.write(f"{turn.start:.2f}\t{turn.end:.2f}\t{speaker}\n")
PY
```

Run batch:

```bash
export HF_TOKEN="PASTE_HUGGINGFACE_TOKEN_HERE"
export HF_HOME=/workspace/hf
export TRANSFORMERS_CACHE=/workspace/hf
export HF_HUB_CACHE=/workspace/hf/hub

/workspace/scripts/batch_transcribe_diarize.sh /workspace/audio/in /workspace/audio/out
```

Download results:

```powershell
rsync -avP `
  -e "ssh -i $env:USERPROFILE\.ssh\runpod_ed25519 -p $env:RUNPOD_SSH_PORT" `
  "root@$env:RUNPOD_SSH_HOST:/workspace/audio/out/" `
  "C:\path\to\results\"
```

### 3i. Cost guardrail

Set 4-hour kill switch immediately after launch.

Inside pod:

```bash
nohup bash -lc 'sleep 4h; runpodctl pod stop "$RUNPOD_POD_ID"' >/workspace/autostop.log 2>&1 &
```

Manual stop from Windows:

```powershell
Invoke-RestMethod `
  -Method Post `
  -Uri "https://rest.runpod.io/v1/pods/$env:RUNPOD_POD_ID/stop" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" }
```

Delete pod after done:

```powershell
Invoke-RestMethod `
  -Method Delete `
  -Uri "https://rest.runpod.io/v1/pods/$env:RUNPOD_POD_ID" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" }
```

Delete network volume only when done with cached models/data:

```powershell
Invoke-RestMethod `
  -Method Delete `
  -Uri "https://rest.runpod.io/v1/networkvolumes/$env:RUNPOD_VOLUME_ID" `
  -Headers @{ Authorization = "Bearer $env:RUNPOD_API_KEY" }
```

Budget rules:

```text
RunPod account:
  keep balance low, e.g. $25-$50
  enable low-balance notifications
  use kill switch every launch
  delete pods after each project
  keep only network volume if cached models are needed
```

## 4. PII carve-out

Clean split:

```text
Local Windows laptop/desktop:
  sensitive mortgage borrower audio
  whisper.cpp local
  optional pyannote local
  encrypted disk only
  no cloud upload

RunPod:
  non-sensitive audio
  LLM experiments
  fine-tuning
  model downloads/checkpoints
```

Local sensitive setup using WSL2:

```powershell
wsl --install -d Ubuntu
```

Inside Ubuntu/WSL2:

```bash
sudo apt-get update
sudo apt-get install -y git cmake build-essential ffmpeg python3-venv python3-pip

mkdir -p ~/ai-local/{models,audio/in,audio/out,repos,venvs}
cd ~/ai-local/repos
git clone https://github.com/ggml-org/whisper.cpp.git
cd whisper.cpp
cmake -B build
cmake --build build -j"$(nproc)"
bash ./models/download-ggml-model.sh medium.en
mv models/ggml-medium.en.bin ~/ai-local/models/
```

CPU local transcription:

```bash
find ~/ai-local/audio/in -type f \( -iname "*.wav" -o -iname "*.mp3" -o -iname "*.m4a" \) | while read -r f; do
  base="$(basename "${f%.*}")"
  wav="$HOME/ai-local/audio/${base}.16k.wav"

  ffmpeg -y -i "$f" -ar 16000 -ac 1 -c:a pcm_s16le "$wav"

  ~/ai-local/repos/whisper.cpp/build/bin/whisper-cli \
    -m ~/ai-local/models/ggml-medium.en.bin \
    -f "$wav" \
    -l en \
    -oj \
    -osrt \
    -of "$HOME/ai-local/audio/out/$base"
done
```

If sensitive audio is more than roughly 50-100 audio-hours/month and turnaround matters:

```text
Buy local GPU.
```

## 5. Strongest argument against Path B

Against cloud GPU:

```text
Cloud ops friction plus PII boundaries can become the real cost.
```

If sensitive borrower calls are frequent, a local RTX 4090 desktop gives:

```text
No third-party data residency issue.
No accidental upload risk.
No forgotten pod billing risk.
No SSH/Jupyter/cloud setup friction.
Always-ready transcription.
Good 7B/13B QLoRA experimentation.
```

Final decision:

```text
Choose Path B now.
Buy local GPU only if sensitive transcription volume is high
or RTX 4090-equivalent cloud use is consistently above ~150 GPU-hours/month.
```
