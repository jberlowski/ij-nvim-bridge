package dev.bridge.fixture.probe;

/** PROBE - navigation in Java, the language the Bridge exists for. */
public class JavaShapes {

    public interface Shape {
        /** The perimeter of this shape, in units. */
        double perimeter();
    }

    public static class Rect implements Shape {
        private final double w;
        private final double h;

        public Rect(double w, double h) {
            this.w = w;
            this.h = h;
        }

        @Override
        public double perimeter() {
            return 2 * (w + h);
        }
    }

    public static class Tri implements Shape {
        @Override
        public double perimeter() {
            return 3.0;
        }
    }

    public static double total(Shape shape) {
        double first = shape.perimeter();
        return first + shape.perimeter();
    }
}
