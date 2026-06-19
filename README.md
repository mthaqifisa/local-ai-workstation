
# 💻 Local AI Workstation

Welcome to the Local AI Workstation repository! This project provides automated shell scripts to provision and manage powerful AI environments locally on your machine. 

---

## 🚀 Author's Machine Specifications
This entire suite is built and optimized for high-performance local AI orchestration. For reference, these scripts are actively run and tested on the following setup:
* **Hardware:** MacBook Pro
* **Processor:** Apple M5 Pro chip
* **Memory:** 64 GB RAM
* **Storage:** 1 TB SSD

---

## 🌿 Choose Your Setup (Branches)
Depending on your project preferences and architectural needs, please select and check out one of the two branches below:

### 🔹 Branch 1: `v1-openClaw` (Legacy Setup)
A robust foundation designed around a standard workspace setup featuring OpenClaw, containerized tools, and essential models.
* **Best for:** Users looking for the classic, baseline workstation build.
* **Main Setup Script:** [`setup_ai_workstation.sh`](https://github.com/mthaqifisa/local-ai-workstation/blob/v1-openClaw/setup_ai_workstation.sh)
* **Documentation:** Read the detailed guide in the [v1-openClaw README](https://github.com/mthaqifisa/local-ai-workstation/blob/v1-openClaw/README.md).

### 🔹 Branch 2: `v2-pythonScript` (Advanced AI Team Setup)
The next evolution of the workstation, transitioning key automation features into powerful Python scripts to manage multi-agent environments and structured orchestration.
* **Best for:** Users who want a more dynamic, Python-driven AI team workspace and improved agent orchestration.
* **Main Setup Script:** [`setup_ai-team.sh`](https://github.com/mthaqifisa/local-ai-workstation/blob/v2-pythonScript/setup_ai-team.sh)
* **Upgrading from v1?** If you previously ran the v1 setup, you must run the safe cleanup utility first to prevent conflicts: [`cleanup_v1-openClaw.sh`](https://github.com/mthaqifisa/local-ai-workstation/blob/v2-pythonScript/cleanup_v1-openClaw.sh)
* **Documentation:** Read the comprehensive details in the [v2-pythonScript README](https://github.com/mthaqifisa/local-ai-workstation/blob/v2-pythonScript/README.md).

---

## 🛠️ Getting Started

1. Clone this repository:
   ```bash
   git clone [https://github.com/mthaqifisa/local-ai-workstation.git](https://github.com/mthaqifisa/local-ai-workstation.git)
   cd local-ai-workstation

```

2. Switch to your desired branch:
```bash
# For Branch 1
git checkout v1-openClaw

# OR For Branch 2
git checkout v2-pythonScript

```


3. Follow the respective branch's README instructions to execute your initialization scripts!

