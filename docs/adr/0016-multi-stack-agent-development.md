# ADR-0016: Multi-stack agent development (Java, Go, TypeScript, Swift, Monkey C)
Status: Accepted (2026-10-21)

## Context
The owner wants the coding agents to build Java, Go, Node.js, Angular, React and React Native projects, iOS apps and
Garmin watch apps, starting projects, resuming half-finished work and fixing issues themselves. The lan-chat runs
showed the main failure on a 27B local model: it guesses library APIs from memory, the compiler answers only
"cannot find symbol", and the agent then guesses again or digs through package internals for hours. The setup was
also web-only: the environment, build and handover stages knew Chrome screenshots and Maven/npm, nothing else.

## Decision
- `law api <name> [member]` answers from the project's exact dependency versions, picked by the nearest project
  marker (`--lang` overrides):
  - Java (pom.xml): `javap -public` over the Maven test classpath (resolved once, cached in target/).
  - Go (go.mod): `go doc` in the module.
  - TypeScript/JavaScript (package.json: Node, Angular, React, React Native): the TypeScript compiler resolves the
    module like the project's build (exports maps, re-exports) and prints the .d.ts declarations. TypeScript 7 is
    a native binary without the JavaScript compiler API, so LAW pins its own TypeScript 6.0.3
    (`api-tools` install step) and uses a project's compiler only when it is 5.x/6.x.
  - Swift (Package.swift, project.yml, *.xcodeproj): the modules the project imports via
    `swift-synthesize-interface -include-submodules` (Objective-C frameworks included; cached per SDK version under
    ~/LAW/cache/apilookup), other SDK modules via their .swiftinterface files, SwiftPM checkouts' public sources.
  - Monkey C (monkey.jungle, manifest.xml): the Connect IQ SDK's own API reference (doc/Toybox).
  Every answer for a name that does not exist lists the closest real names.
- The coder prompt makes `law api` the way to learn an API in every stack; the library-dig guard also counts
  reading node_modules, the Go module cache, SwiftPM checkouts and the Xcode SDK.
- Toolchains in the Brewfile: go, xcodegen, cocoapods, watchman, casks connectiq and connectiq-sdk-manager (Xcode and
  its simulators are installed by the owner).
- Per-platform instructions in the environment, development and handover stages:
  - iOS: XcodeGen `project.yml` instead of hand-edited `project.pbxproj` (agents cannot use the Xcode GUI and
    corrupt pbxproj files by hand); gate `xcodebuild test` on a recorded simulator destination; screenshots with
    `xcrun simctl io booted screenshot`.
  - React Native: Expo (`create-expo-app`, TypeScript) unless the spec asks for bare React Native, because Expo's
    generated native projects need no hand-maintained iOS/Android folders; iOS simulator only (no Android SDK).
  - Go: gofmt, go vet, go test.
  - Garmin: monkeyc per target device with the machine's developer key (~/LAW/state/keys, never in a project),
    (:test) unit tests in the simulator; screenshots of the simulator window with peekaboo.

## Consequences
One lookup command across stacks instead of per-language habits. Verified 2026-10-21 after the owner signed in to the SDK Manager: 178 devices installed; the SDK's Analog sample built for fenix7, ran in the simulator (`open -a ConnectIQ`, `monkeydo`) and was captured with peekaboo (window app name "Connect IQ Device Simulator"); macOS asked once to confirm opening ConnectIQ.app (Gatekeeper), which the setup session approved. Garmin device definitions need the owner's Garmin
sign-in in the SDK Manager once; until then the environment stage stops with that instruction. Android is out of
scope until an Android SDK is installed. The first Swift lookup per SDK version takes ~25 s (interface synthesis),
later ones ~0.3 s.
