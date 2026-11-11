package demo;

import java.util.ArrayList;
import java.util.List;

public class Counter {
    private final List<String> seen = new ArrayList<>();
    private int total;

    public int add(String word) {
        if (word == null) {
            throw new IllegalArgumentException("word is null");
        }
        seen.add(word);
        total += word.length();
        return total;
    }

    public int safeAdd(String word) {
        try {
            return add(word);
        } catch (IllegalArgumentException e) {
            return -1;
        }
    }

    public List<String> seen() {
        return seen;
    }

    public static int twice(int x) {
        return 2 * x;
    }
}
