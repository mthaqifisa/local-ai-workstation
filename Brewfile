# LAW v6 system packages. `law install` runs `brew bundle` with this file (idempotent).
tap "jundot/omlx", "https://github.com/jundot/omlx"
brew "uv"
brew "git"
brew "git-lfs"
brew "just"
brew "jq"
brew "ffmpeg"
brew "valkey"
brew "caddy"
brew "colima"
brew "docker"
brew "docker-compose"
brew "node"
brew "openjdk@25"   # LTS JDK for Java/Spring projects (keg-only; LAW puts it on PATH)
brew "maven"
# Stacks the coding agents build (ADR-0016): Go, iOS (XcodeGen generates the Xcode project from project.yml),
# React Native on iOS (CocoaPods, Watchman) and Garmin watch apps (Connect IQ SDK: monkeyc, simulator).
brew "go"
brew "xcodegen"
brew "cocoapods"
brew "watchman"
brew "pandoc"
brew "typst"
brew "graphviz"
brew "mermaid-cli"   # diagrams in documents (/proposal); its headless browser is the doc-tools step
brew "tesseract"
brew "cliclick"
brew "peekaboo"
brew "opencode"
brew "gitleaks"
brew "shellcheck"
brew "jundot/omlx/omlx", args: ["with-custom-kernel"]
cask "libreoffice"
cask "connectiq"
cask "connectiq-sdk-manager"   # downloads Garmin device definitions (needs the owner's Garmin sign-in once)
