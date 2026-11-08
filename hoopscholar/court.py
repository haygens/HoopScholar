"""Half-court geometry in NBA.com shot-chart coordinates.

Units are tenths of a foot. The hoop is at (0, 0), the baseline at y = -47.5,
half court at y = 422.5, and the sidelines at x = +/-250.
"""
import math

HOOP_R = 7.5
RA_R = 40            # restricted area: 4 ft
THREE_R = 237.5      # 23' 9"
CORNER_X = 220       # corner three is 22 ft
CORNER_Y = 92.5      # where the corner line meets the arc (14 ft from the baseline)
PAINT_HALF_W = 80    # 16 ft lane
FT_LINE_Y = 142.5    # 19 ft from the baseline


def draw_half_court(ax=None, color="#333", lw=1.5, outer_lines=True):
    """Draw an NBA half court on a matplotlib Axes and return it."""
    import matplotlib.pyplot as plt
    from matplotlib.patches import Arc, Circle, Rectangle

    ax = ax or plt.gca()
    parts = [
        Circle((0, 0), radius=HOOP_R, lw=lw, color=color, fill=False),            # hoop
        Rectangle((-30, -7.5), 60, -1, lw=lw, color=color),                        # backboard
        Rectangle((-PAINT_HALF_W, -47.5), 160, 190, lw=lw, color=color, fill=False),
        Rectangle((-60, -47.5), 120, 190, lw=lw, color=color, fill=False),
        Arc((0, FT_LINE_Y), 120, 120, theta1=0, theta2=180, lw=lw, color=color),
        Arc((0, FT_LINE_Y), 120, 120, theta1=180, theta2=0, lw=lw, color=color, linestyle="dashed"),
        Arc((0, 0), 2 * RA_R, 2 * RA_R, theta1=0, theta2=180, lw=lw, color=color),
        Rectangle((-CORNER_X, -47.5), 0, 140, lw=lw, color=color),                # corner 3 lines
        Rectangle((CORNER_X, -47.5), 0, 140, lw=lw, color=color),
        Arc((0, 0), 2 * THREE_R, 2 * THREE_R, theta1=22, theta2=158, lw=lw, color=color),
        Arc((0, 422.5), 120, 120, theta1=180, theta2=0, lw=lw, color=color),     # center circle
        Arc((0, 422.5), 40, 40, theta1=180, theta2=0, lw=lw, color=color),
    ]
    if outer_lines:
        parts.append(Rectangle((-250, -47.5), 500, 470, lw=lw, color=color, fill=False))
    for p in parts:
        ax.add_patch(p)
    ax.set_xlim(-250, 250)
    ax.set_ylim(-47.5, 422.5)
    ax.set_aspect("equal")
    ax.axis("off")
    return ax


def classify(x: float, y: float) -> tuple[str, str, str, int]:
    """Return (zone_basic, zone_area, zone_range, distance_ft) for a location,
    matching the labels NBA.com uses. Used for demo data and sanity checks."""
    dist = math.hypot(x, y)
    dist_ft = int(round(dist / 10))
    if y > 422.5:
        basic = "Backcourt"
    elif abs(x) >= CORNER_X and y <= CORNER_Y:
        basic = "Left Corner 3" if x < 0 else "Right Corner 3"
    elif dist >= THREE_R and y > CORNER_Y:
        basic = "Above the Break 3"
    elif dist <= RA_R:
        basic = "Restricted Area"
    elif abs(x) < PAINT_HALF_W and y < FT_LINE_Y:
        basic = "In The Paint (Non-RA)"
    else:
        basic = "Mid-Range"

    angle = math.degrees(math.atan2(y, x))  # 90 = straight on
    if basic in ("Restricted Area", "In The Paint (Non-RA)") and abs(x) < 80:
        area = "Center(C)"
    elif angle > 120 or (x < 0 and y <= CORNER_Y):
        area = "Left Side(L)"
    elif angle > 100:
        area = "Left Side Center(LC)"
    elif angle >= 80:
        area = "Center(C)"
    elif angle >= 60:
        area = "Right Side Center(RC)"
    else:
        area = "Right Side(R)"

    if dist_ft < 8:
        rng = "Less Than 8 ft."
    elif dist_ft < 16:
        rng = "8-16 ft."
    elif dist_ft < 24:
        rng = "16-24 ft."
    else:
        rng = "24+ ft."
    return basic, area, rng, dist_ft
