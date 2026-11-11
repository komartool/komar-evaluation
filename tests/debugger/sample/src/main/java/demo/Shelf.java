package demo;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.TreeMap;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicReference;

/** Containers of project objects (Tag), for the check that the debugger runs no code of the program. */
public class Shelf {
    private final Map<Tag, Integer> counts = new HashMap<>();
    private final Map<String, Tag> byName = new LinkedHashMap<>();
    private final Set<Tag> tags = new HashSet<>();
    private final Map<Tag, String> sorted = new TreeMap<>();
    private final Map<String, Integer> plain = new ConcurrentHashMap<>();
    private final List<Tag> order = new ArrayList<>();
    private final AtomicReference<Tag> last = new AtomicReference<>();
    private Optional<Tag> first = Optional.empty();
    private final List<String> fixed = List.of("a", "b", "c");
    private final Thread owner = Thread.currentThread();
    private final ClassLoader loader = Shelf.class.getClassLoader();

    public int put(String name) {
        Tag t = new Tag(name);
        counts.merge(t, 1, Integer::sum);
        byName.put(name, t);
        tags.add(t);
        sorted.put(t, name);
        plain.merge(name, 1, Integer::sum);
        order.add(t);
        last.set(t);
        if (!first.isPresent()) first = Optional.of(t);
        return order.size();
    }

    public int fixedSize() {
        int n = fixed.size();
        return n;
    }
}
