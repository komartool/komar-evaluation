package demo;

/** A key whose hashCode, equals and toString leave a marker on standard error: the debugger must never run them. */
public final class Tag implements Comparable<Tag> {
    /** Set by the scenario once the program has stopped using tags: a marker after that is the debugger's. */
    public static volatile boolean armed;

    private final String name;

    public Tag(String name) {
        this.name = name;
    }

    @Override
    public int hashCode() {
        if (armed) System.err.println("PROJECT_HASHCODE_CALLED");
        return name.hashCode();
    }

    @Override
    public boolean equals(Object o) {
        if (armed) System.err.println("PROJECT_EQUALS_CALLED");
        return o instanceof Tag && ((Tag) o).name.equals(name);
    }

    @Override
    public String toString() {
        if (armed) System.err.println("PROJECT_TOSTRING_CALLED");
        return "Tag(" + name + ")";
    }

    @Override
    public int compareTo(Tag o) {
        if (armed) System.err.println("PROJECT_COMPARETO_CALLED");
        return name.compareTo(o.name);
    }
}
