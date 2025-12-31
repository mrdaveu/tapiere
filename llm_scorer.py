"""
LLM-based fit scoring using Alibaba DashScope with Qwen models.
Analyzes item descriptions against user's sizing profile.
"""

import os
from typing import Optional

# Try to import OpenAI client for DashScope compatibility
try:
    from openai import OpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False
    print("[LLM Scorer] openai package not installed. Fit scoring disabled.")

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
DASHSCOPE_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"

MEASUREMENT_LABELS = {
    'a': 'Shoulder width (肩幅)',
    'b': 'Chest width (身幅)',
    'c': 'Length (着丈)',
    'd': 'Waist width (ウエスト)',
    'e': 'Hip width (ヒップ)',
    'f': 'Rise (股上)',
    'g': 'Inseam (股下)',
    'h': 'Reserved'
}

OPERATOR_MEANINGS = {
    '>': 'at least',
    '<': 'at most',
    '~': 'approximately'
}


def build_sizing_prompt(sizing_profile: dict) -> str:
    """Convert sizing profile dict to natural language preferences."""
    preferences = []
    for key in ['a', 'b', 'c', 'd', 'e', 'f', 'g', 'h']:
        op_key = f'size_{key}_op'
        val_key = f'size_{key}_val'
        op = sizing_profile.get(op_key)
        val = sizing_profile.get(val_key)
        if op and val:
            label = MEASUREMENT_LABELS.get(key, key.upper())
            meaning = OPERATOR_MEANINGS.get(op, '')
            preferences.append(f"- {label}: {meaning} {val}cm")
    return '\n'.join(preferences) if preferences else "No specific measurements specified"


def score_item_fit(item_description: str, sizing_profile: dict) -> Optional[int]:
    """
    Call Qwen to score how well an item fits the sizing profile.

    Args:
        item_description: The item's description text (Japanese)
        sizing_profile: Dict with keys like 'size_a_op', 'size_a_val', etc.

    Returns:
        4 = Great fit (measurements clearly match preferences)
        3 = Acceptable fit (measurements are close enough)
        2 = Mediocre fit (some measurements are off)
        1 = Poor fit (measurements clearly don't match)
        None = Error or unable to determine
    """
    if not HAS_OPENAI or not DASHSCOPE_API_KEY:
        return None

    sizing_text = build_sizing_prompt(sizing_profile)
    if sizing_text == "No specific measurements specified":
        return None  # No sizing profile configured

    prompt = f"""You are a clothing fit analyzer. Given a user's body measurement preferences and a Japanese clothing item description, rate how well this item would fit.

User's measurement preferences:
{sizing_text}

Item description:
{item_description[:2000]}

Rate the fit on this scale:
4 = Great fit (measurements clearly match preferences)
3 = Acceptable fit (measurements are close enough, within 2-3cm)
2 = Mediocre fit (some measurements are notably off)
1 = Poor fit (measurements clearly don't match preferences)

If the description doesn't include relevant measurements (like 身幅, 着丈, 肩幅, etc.), make your best guess based on size labels (S/M/L, I/II/III, FREE SIZE) or respond with 0 if truly unable to determine.

Important: Many Japanese listings use Roman numerals (I=S, II=M, III=L) or mention measurements in the description like "身幅52 着丈65".

Respond with ONLY a single digit (0, 1, 2, 3, or 4). No explanation."""

    try:
        client = OpenAI(
            api_key=DASHSCOPE_API_KEY,
            base_url=DASHSCOPE_BASE_URL,
        )

        completion = client.chat.completions.create(
            model="qwen-turbo",  # Fast and cheap
            messages=[{"role": "user", "content": prompt}],
            max_tokens=5,
            temperature=0.1,
        )

        score_text = completion.choices[0].message.content.strip()
        # Extract first digit from response
        for char in score_text:
            if char.isdigit():
                score = int(char)
                return score if 1 <= score <= 4 else None
        return None

    except Exception as e:
        print(f"[LLM Scorer] Error: {e}")
        return None


def score_item_fit_sync(item_description: str, sizing_profile: dict) -> Optional[int]:
    """Synchronous wrapper for score_item_fit."""
    return score_item_fit(item_description, sizing_profile)


if __name__ == "__main__":
    # Test the scorer
    test_profile = {
        'size_a_op': '~', 'size_a_val': 44,  # Shoulder ~44cm
        'size_b_op': '~', 'size_b_val': 52,  # Chest ~52cm
        'size_c_op': '>', 'size_c_val': 65,  # Length >65cm
    }

    test_description = """
    MHL. マーガレットハウエル
    サイズ表記：II
    身幅52cm
    着丈68cm
    肩幅44cm
    状態良好
    """

    print("Testing LLM scorer...")
    print(f"Profile:\n{build_sizing_prompt(test_profile)}")
    print(f"\nDescription excerpt:\n{test_description[:200]}")

    score = score_item_fit(test_description, test_profile)
    print(f"\nFit score: {score}")
