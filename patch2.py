import sys
with open('src/emailgenius/campaign.py', 'r') as f:
    lines = f.readlines()

loop_start_idx = -1
for i, line in enumerate(lines):
    if line.startswith('    for lead_row in lead_rows:'):
        loop_start_idx = i
        break

loop_end_idx = -1
for i in range(loop_start_idx + 1, len(lines)):
    # The next thing unindented
    if line.strip() and not line.startswith('        ') and not line.startswith('    for lead_row'):
        pass
    if lines[i].startswith('    if export_rows:'):
        loop_end_idx = i
        break

print(f"Loop from {loop_start_idx} to {loop_end_idx}")

# I will replace the loop with a function
func_def = [
    "    workers = min(max_concurrency, 8)\n",
    "    semaphore = threading.Semaphore(workers)\n",
    "    import concurrent.futures\n",
    "    import threading\n",
    "    _lock = threading.Lock()\n",
    "\n",
    "    def worker(lead_row):\n",
    "        with semaphore:\n"
]

loop_body = []
for i in range(loop_start_idx + 1, loop_end_idx):
    # Change continue to return
    line = lines[i]
    if line.strip() == "continue":
        line = line.replace("continue", "return")
    
    # We need to lock operations on shared counters and lists
    if line.strip() in [
        "warnings_total += 1",
        "rows_failed += 1",
        "llm_items_attempted += 1",
        "rows_generated_ok += 1",
        "processed_companies += 1",
    ] or line.strip().startswith("export_rows.append") or line.strip().startswith("drive_export_items.append"):
        loop_body.append("        with _lock:\n")
        loop_body.append("    " + line)
    else:
        # Increase indent
        loop_body.append("    " + line)

func_def.extend(loop_body)
func_def.append("        return\n\n")
func_def.append("    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:\n")
func_def.append("        futures = [executor.submit(worker, r) for r in lead_rows]\n")
func_def.append("        concurrent.futures.wait(futures)\n")

new_lines = lines[:loop_start_idx] + func_def + lines[loop_end_idx:]

with open('src/emailgenius/campaign.py', 'w') as f:
    f.writelines(new_lines)
