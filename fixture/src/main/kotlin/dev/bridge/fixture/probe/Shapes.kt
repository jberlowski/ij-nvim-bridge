package dev.bridge.fixture.probe

/**
 * PROBE - navigation. A known shape for the harness to navigate: one interface,
 * two implementations, and callers. Tests find positions by searching this
 * text, so it can be edited, but keep the names.
 */
interface Shape {
    /** The area of this shape, in square units. */
    fun area(): Double
}

class Circle(private val radius: Double) : Shape {
    override fun area(): Double = Math.PI * radius * radius
}

class Square(private val side: Double) : Shape {
    override fun area(): Double = side * side
}

fun describe(shape: Shape): String {
    val measured = shape.area()
    var total = measured
    total += shape.area()
    return "area=$total"
}
