# Agent Quality Improvement Plan — Phase 4

## Objective
Push all 15 local MLX agents to near Fable-5/Devin/Kimi-K3 quality through prompt engineering, not model training.

## What Claude Already Did (Phases 1-3)
- Phase 1: Service recovery + baseline testing of all agents
- Phase 2: Overhauled all 15 system prompts with structured workflows
- Phase 3: Iterative improvements (v2-v5 on orchestrator, multiple retests)

## What's Still Missing (The Gap)

### 1. No Few-Shot Examples (Critical)
None of the 15 personas have concrete input→output examples. This is the #1 improvement for LLM performance. Adding 2-3 examples per persona can improve accuracy by 20-40%.

### 2. No Self-Critique Loops (Except Reasoner)
Only reasoner/deep_reasoner have self-critique. Every agent should review its own output before delivering.

### 3. No Failure Recovery Protocols
When a tool call fails, agents have no explicit recovery steps. They just report the error.

### 4. No Context Management
When conversations get long, agents lose track. No explicit instructions for summarizing, truncating, or refocusing.

### 5. Per-Model Prompt Mismatch
The prompts are generic — not tuned to each model's actual strengths:
- `qwen3.6-35b` (orchestrator): Good at planning, weak at long context
- `qwen3-coder-next` (coder): Good at code, weak at edge cases
- `deepseek-r1-70b` (reasoner): Excellent at reasoning, verbose
- `qwen3.6-27b` (general/qa/writer): Weakest model — needs narrow scope + examples

### 6. No Evaluation Framework
No benchmark suite to measure improvement over time.

## Improvement Strategy

### Step 1: Strengthen BASE_SYSTEM
Add:
- Explicit failure recovery protocol
- Context management rules
- Anti-hallucination examples (what NOT to do)

### Step 2: Add Few-Shot Examples to Every Persona
Each persona gets 2-3 concrete examples showing:
- Input → Tool calls → Output format
- Common mistakes and how to avoid them

### Step 3: Add Self-Critique to Every Persona
Every agent must:
1. Generate output
2. Review against quality criteria
3. Fix issues before delivering

### Step 4: Per-Model Optimization
Tune prompts to each model's actual capabilities.

### Step 5: Build Evaluation Harness
Create a benchmark suite that tests each agent on standardized tasks.

## Files to Modify
1. `mlx-agent.py` — BASE_SYSTEM + all 15 persona prompts
2. New file: `enhancement/benchmark_suite.py` — evaluation harness

## Success Criteria
- Every persona has ≥2 few-shot examples
- Every persona has self-critique step
- Every persona has failure recovery protocol
- Benchmark suite tests all 15 agents on standardized tasks