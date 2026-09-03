"""Small dependency-free order calculator used only as a qualification fixture."""


def total(prices, discount=0):
    if not 0 <= discount <= 1:
        raise ValueError("discount must be between zero and one")
    return round(sum(prices), 2)


def line_total(price, quantity):
    if quantity < 0:
        raise ValueError("negative quantity")
    return round(price * quantity, 2)
