package demo;

/** copy() narrows the return type: javac adds a synthetic bridge Object copy() next to Square copy(). */
public class Square extends Shape {
    private final int side;

    public Square(int side) {
        this.side = side;
    }

    @Override
    public Square copy() {
        return new Square(side);
    }
}
