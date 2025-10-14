from rgbmatrix import RGBMatrix
import debug
from data.data_test import DataTest
from data.data import Data
from utils import args, led_matrix_options
from data.scoreboard_config import ScoreboardConfig
from renderer.main import MainRenderer
from renderer.main_test import MainRendererTest
import os
import sys
import shlex
import json

# --- Default CLI arguments injection ---
# 1) Read default LED args from config.json (section: "matrix" or "led")
# 2) Then apply FFS_DEFAULT_ARGS env var (overrides config defaults)
# 3) Finally, any real CLI args override both
def _defaults_from_config():
    candidates = [
        os.path.join(os.path.dirname(__file__), 'config.json'),
        os.path.join(os.getcwd(), 'config.json'),
    ]
    for p in candidates:
        try:
            if not os.path.exists(p):
                continue
            with open(p, 'r') as f:
                cfg = json.load(f)
            matrix = cfg.get('matrix') or cfg.get('led') or {}
            if not isinstance(matrix, dict):
                matrix = {}
            defaults = []
            # Map known keys to rpi-rgb-led-matrix flags
            if 'gpio_mapping' in matrix:
                defaults.append(f"--led-gpio-mapping={matrix['gpio_mapping']}")
            if 'brightness' in matrix:
                defaults.append(f"--led-brightness={matrix['brightness']}")
            if 'slowdown_gpio' in matrix:
                defaults.append(f"--led-slowdown-gpio={matrix['slowdown_gpio']}")
            if 'rgb_sequence' in matrix:
                defaults.append(f"--led-rgb-sequence={matrix['rgb_sequence']}")
            if 'rows' in matrix:
                defaults.append(f"--led-rows={matrix['rows']}")
            if 'cols' in matrix:
                defaults.append(f"--led-cols={matrix['cols']}")
            if 'chain' in matrix or 'chain_length' in matrix:
                chain = matrix.get('chain', matrix.get('chain_length'))
                defaults.append(f"--led-chain={chain}")
            if 'parallel' in matrix:
                defaults.append(f"--led-parallel={matrix['parallel']}")
            if 'pwm_bits' in matrix:
                defaults.append(f"--led-pwm-bits={matrix['pwm_bits']}")
            if 'pixel_mapper' in matrix:
                defaults.append(f"--led-pixel-mapper={matrix['pixel_mapper']}")
            return defaults
        except Exception:
            continue
    return []

cfg_defaults = _defaults_from_config()
if cfg_defaults:
    # Insert config defaults first so later sources can override them
    sys.argv[1:1] = cfg_defaults

DEFAULT_ARG_STRING = os.getenv("FFS_DEFAULT_ARGS", "").strip()
if DEFAULT_ARG_STRING:
    # Insert env defaults after config defaults (still before real CLI),
    # so env overrides config but is overridden by actual CLI args.
    insertion_index = 1 + len(cfg_defaults)
    sys.argv[insertion_index:insertion_index] = shlex.split(DEFAULT_ARG_STRING)
elif len(sys.argv) == 1 and not cfg_defaults:
    # No CLI args and no config/env defaults: fall back to minimal sane defaults
    sys.argv[1:1] = [
        # "--led-gpio-mapping=regular",
        # "--led-brightness=60",
        # "--led-slowdown-gpio=2",
        # "--led-rgb-sequence=RBG",
    ]
# --- end defaults injection ---

args = args()
# Read scoreboard options from config.json if it exists
config = ScoreboardConfig("config", args)


SCRIPT_NAME = "Fantasy Football Scoreboard"
SCRIPT_VERSION = "1.0.0"

# Get supplied command line arguments


# Check for led configuration arguments
matrixOptions = led_matrix_options(args)

# Initialize the matrix
matrix = RGBMatrix(options=matrixOptions)

# Print some basic info on startup
debug.info("{} - v{} ({}x{})".format(SCRIPT_NAME,
           SCRIPT_VERSION, matrix.width, matrix.height))

debug.set_debug_status(config)

if config.testing:
    debug.info('testing')
    data = DataTest(config)
    MainRendererTest(matrix, data).render()
else:
    data = Data(config)
    MainRenderer(matrix, data).render()
