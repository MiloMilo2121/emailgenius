import re
with open('src/emailgenius/campaign.py', 'r') as f:
    code = f.read()

# Add max_concurrency to signature
code = code.replace(
    '    workspace_folder_id: str | None,',
    '    workspace_folder_id: str | None,\n    max_concurrency: int = 1,'
)

# Add max_concurrency to caller
code = code.replace(
    '            workspace_folder_id=workspace_folder_id or config.workspace_folder_id,',
    '            workspace_folder_id=workspace_folder_id or config.workspace_folder_id,\n            max_concurrency=max_concurrency,'
)

# Find the loop and refactor it
loop_start = '    for lead_row in lead_rows:'
loop_end = '                }\n            )\n\n' # End of the drive_export_items.append block

if loop_start in code:
    print("Found loop start")
else:
    print("Loop start not found")

with open('src/emailgenius/campaign.py', 'w') as f:
    f.write(code)

