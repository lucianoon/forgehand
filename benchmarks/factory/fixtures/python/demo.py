import json
from pathlib import Path
from orders import total

config = json.loads(Path("config.json").read_text())
print(f"{config['currency']} {total([5, 7]):.2f}")
