#!/bin/bash
# <xbar.title>LAW — Local AI Workstation</xbar.title>
# <xbar.desc>Worker state, loaded model, queue, memory pressure and free disk in the menu bar.</xbar.desc>
# <xbar.dependencies>jq, curl, law</xbar.dependencies>
# <swiftbar.hideAbout>true</swiftbar.hideAbout><swiftbar.hideRunInTerminal>true</swiftbar.hideRunInTerminal>
#
# Install: brew install --cask swiftbar   (or xbar), then copy/symlink this file into the plugin folder:
#   ln -s "$PWD/extras/swiftbar/law.10s.sh" ~/Library/Application\ Support/SwiftBar/Plugins/
# Refreshes every 10 s (the "10s" in the file name).
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
GW="${LAW_GATEWAY:-http://127.0.0.1:4000}"
DASH="${LAW_DASHBOARD:-http://127.0.0.1:8800}"
LAW="$(command -v law || echo "$HOME/.local/bin/law")"

h="$(curl -s -m 2 "$GW/health" 2>/dev/null)"
if [ -z "$h" ]; then
  echo "◌ LAW | color=gray"
  echo "---"
  echo "Gateway not answering | color=red"
  echo "Start services | bash=$LAW param1=start terminal=false refresh=true"
  exit 0
fi
state="$(echo "$h" | jq -r '.worker.state // "stopped"')"
model="$(echo "$h" | jq -r '.worker.model // "-"')"
queued="$(echo "$h" | jq -r '.queued // 0')"
job="$(echo "$h" | jq -r '.worker.job_id // empty')"
pressure="$(sysctl -n kern.memorystatus_vm_pressure_level 2>/dev/null)"
case "$pressure" in 4) mem="critical"; mcol=red ;; 2) mem="warn"; mcol=orange ;; *) mem="normal"; mcol=green ;; esac
free="$(df -g /System/Volumes/Data 2>/dev/null | awk 'NR==2{print $4}')"

icon="●"; [ "$state" = "running" ] && icon="◉"; [ "$state" = "stopped" ] && icon="○"
title="$icon LAW"; [ "$queued" != "0" ] && title="$title ·$queued"
[ "$mem" = "critical" ] && title="$title ⚠"
echo "$title"
echo "---"
echo "Worker: $state${job:+ (job $job)}"
echo "Model: $model"
echo "Queue: $queued waiting"
echo "Memory pressure: $mem | color=$mcol"
echo "Free disk: ${free:-?} GB"
echo "---"
echo "Open dashboard | href=$DASH"
echo "Unload model (free RAM) | bash=$LAW param1=unload terminal=false refresh=true"
echo "Restart services | bash=$LAW param1=restart terminal=false refresh=true"
echo "Stop services | bash=$LAW param1=stop terminal=false refresh=true"
