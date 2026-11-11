import java.io.BufferedWriter;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.OutputStreamWriter;
import java.lang.reflect.Array;
import java.lang.reflect.Field;
import java.lang.reflect.Modifier;
import java.nio.charset.StandardCharsets;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collection;
import java.util.Deque;
import java.util.IdentityHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicLong;

/**
 * Recorder called from the trusted BTrace script (see TargetTrace.java.tmpl). Lives on the boot class
 * path so that it is visible from every class loader. Writes one JSON object per line:
 *
 *   {"ev":"enter","id":1,"thread":7,"caller":"a.FooTest","caller_effective":"a.FooTest",
 *    "via_scenario":false,"self":{...},"args":[...]}
 *   {"ev":"call","id":1,"sig":"java.util.List#add(Ljava/lang/Object;)Z"}
 *   {"ev":"return","id":1,"value":{...},"void":false}
 *   {"ev":"throw","id":1,"exception":"java.lang.IllegalStateException"}
 *
 * caller is the class of the frame right above the target; caller_effective the first frame above
 * it that is not a mock, proxy, lambda or reflection frame; via_scenario tells whether the scenario
 * test class (system property state.trace.scenario) is on the stack.
 *
 * An exception leaving the target is reported by Kind.ERROR (error()), with its class. As a
 * fallback, an open invocation whose stack depth is no longer on the thread's stack is closed as
 * "throw" without a class.
 *
 * Values are serialised as a graph of fields up to depth D (system property state.trace.depth),
 * with identity-based cycle detection and a per-value node budget (state.trace.budget).
 */
public final class StateTrace {
    private static final int DEPTH = Integer.getInteger("state.trace.depth", 5);
    private static final int BUDGET = Integer.getInteger("state.trace.budget", 4000);
    private static final int MAX_ELEMENTS = Integer.getInteger("state.trace.elements", 200);
    private static final AtomicLong IDS = new AtomicLong();
    private static final ThreadLocal<Deque<long[]>> OPEN = ThreadLocal.withInitial(ArrayDeque::new);
    private static final ThreadLocal<Boolean> BUSY = ThreadLocal.withInitial(() -> Boolean.FALSE);
    private static final java.util.Set<Long> ALL_OPEN = java.util.concurrent.ConcurrentHashMap.newKeySet();
    /**
     * The agent reaches every JVM of a command through JAVA_TOOL_OPTIONS, including the Maven launcher
     * JVM itself, where build plugins may load their own copy of the target class (jgit: the build
     * walks .gitignore files with JGit). Only test JVMs (surefire forks) are recorded: a JVM whose main
     * class is a build launcher records nothing. The list can be overridden with state.trace.skipMain
     * (comma-separated prefixes of sun.java.command).
     */
    private static final String SCENARIO = System.getProperty("state.trace.scenario", "");
    private static final String MAIN = mainCommand();
    private static final boolean DISABLED = isLauncher(MAIN);
    private static BufferedWriter out;

    private static String mainCommand() {
        try {
            String cmd = System.getProperty("sun.java.command", "");
            int sp = cmd.indexOf(' ');
            return sp < 0 ? cmd : cmd.substring(0, sp);
        } catch (Throwable t) {
            return "";
        }
    }

    private static boolean isLauncher(String main) {
        String list = System.getProperty("state.trace.skipMain",
            "org.codehaus.plexus.classworlds.,org.apache.maven.wrapper.,org.apache.maven.cli.,"
            + "org.gradle.,worker.org.gradle.");
        for (String prefix : list.split(",")) {
            if (!prefix.isEmpty() && main.startsWith(prefix.trim())) return true;
        }
        return false;
    }

    private StateTrace() {}

    private static synchronized void write(String line) {
        try {
            if (out == null) {
                String dir = System.getProperty("state.trace.dir", System.getProperty("java.io.tmpdir"));
                new File(dir).mkdirs();
                // No Runtime.getRuntime()/ManagementFactory here: the first event may arrive while the
                // test has Runtime statically mocked (Checkstyle MainTest uses mockStatic(Runtime.class)).
                // An exception escaping the probe makes BTrace drop the event, so nothing was ever written.
                long pid = ProcessHandle.current().pid();
                File f = new File(dir, "trace-" + pid + "-" + System.nanoTime() + ".jsonl");
                out = new BufferedWriter(new OutputStreamWriter(new FileOutputStream(f), StandardCharsets.UTF_8));
                out.write("{\"ev\":\"jvm\",\"pid\":" + pid + ",\"main\":" + Graph.quote(MAIN) + "}\n");
                realRuntime().addShutdownHook(new Thread(StateTrace::shutdown));
            }
            out.write(line);
            out.write('\n');
            out.flush();
        } catch (Throwable ignored) {
        }
    }

    /** The JVM's Runtime, bypassing a possibly mocked Runtime.getRuntime(). */
    private static Runtime realRuntime() {
        try {
            Field f = Runtime.class.getDeclaredField("currentRuntime");
            f.setAccessible(true);
            return (Runtime) f.get(null);
        } catch (Throwable t) {
            return Runtime.getRuntime();
        }
    }

    private static void shutdown() {
        // invocations still open at exit did not return normally
        synchronized (StateTrace.class) {
            try {
                if (out != null) {
                    for (Long id : ALL_OPEN) {
                        out.write("{\"ev\":\"throw\",\"id\":" + id + ",\"atExit\":true}\n");
                    }
                    out.flush();
                }
            } catch (IOException ignored) {
            }
        }
    }

    private static int depth() {
        return new Throwable().getStackTrace().length;
    }

    /** Closes every open invocation of this thread that is deeper than or as deep as {@code depth}. */
    private static void closeStale(Deque<long[]> open, int depth, boolean inclusive) {
        while (!open.isEmpty()) {
            long[] top = open.peek();
            if (top[1] > depth || (inclusive && top[1] == depth)) {
                open.pop();
                ALL_OPEN.remove(top[0]);
                write("{\"ev\":\"throw\",\"id\":" + top[0] + "}");
            } else {
                break;
            }
        }
    }

    /** A frame of the tracer: its classes, or a handler BTrace copied into the traced class (on JDK 11 the
     *  copy, e.g. Owner.$btrace$...$onEntry, is a frame of its own between the tracer and the target). */
    private static boolean helperFrame(StackTraceElement e) {
        return helperFrame(e.getClassName()) || e.getMethodName().startsWith("$btrace$");
    }

    private static boolean helperFrame(String cls) {
        return cls.startsWith("org.openjdk.btrace.") || cls.equals("StateTrace") || cls.startsWith("StateTrace$")
            || cls.startsWith("TargetTrace") || cls.startsWith("$btrace");
    }

    private static boolean indirectFrame(String cls) {
        return Graph.opaqueType(cls) || cls.startsWith("java.lang.reflect.") || cls.startsWith("jdk.internal.reflect.")
            || cls.startsWith("java.lang.invoke.") || cls.startsWith("sun.reflect.") || cls.startsWith("org.mockito.")
            || cls.startsWith("net.bytebuddy.") || cls.contains("$$Lambda") || cls.contains("$MockitoMock$");
    }

    /** caller, caller_effective and via_scenario of the current invocation (see the class comment). */
    private static void appendCaller(StringBuilder sb) {
        StackTraceElement[] st = new Throwable().getStackTrace();
        int i = 0;
        while (i < st.length && helperFrame(st[i])) i++;
        // st[i] is the target method itself; its caller is the next frame
        String caller = i + 1 < st.length ? st[i + 1].getClassName() : null;
        String effective = null;
        boolean viaScenario = false;
        for (int j = i + 1; j < st.length; j++) {
            String cls = st[j].getClassName();
            if (effective == null && !indirectFrame(cls)) effective = cls;
            if (!SCENARIO.isEmpty() && (cls.equals(SCENARIO) || cls.startsWith(SCENARIO + "$"))) viaScenario = true;
        }
        sb.append(",\"caller\":").append(caller == null ? "null" : Graph.quote(caller))
          .append(",\"caller_effective\":").append(effective == null ? "null" : Graph.quote(effective))
          .append(",\"via_scenario\":").append(viaScenario);
    }

    public static void enter(Object self, Object[] args) {
        if (DISABLED || BUSY.get()) return;
        BUSY.set(Boolean.TRUE);
        try {
            int d = depth();
            Deque<long[]> open = OPEN.get();
            closeStale(open, d, true);
            long id = IDS.incrementAndGet();
            StringBuilder sb = new StringBuilder(256);
            sb.append("{\"ev\":\"enter\",\"id\":").append(id)
              .append(",\"thread\":").append(Thread.currentThread().getId());
            appendCaller(sb);
            sb.append(",\"self\":");
            new Graph().value(sb, self, 0);
            sb.append(",\"args\":[");
            if (args != null) {
                for (int i = 0; i < args.length; i++) {
                    if (i > 0) sb.append(',');
                    new Graph().value(sb, args[i], 0);
                }
            }
            sb.append("]}");
            write(sb.toString());
            open.push(new long[] {id, d});
            ALL_OPEN.add(id);
        } catch (Throwable t) {
            write("{\"ev\":\"error\",\"where\":\"enter\",\"msg\":" + Graph.quote(String.valueOf(t)) + "}");
        } finally {
            BUSY.set(Boolean.FALSE);
        }
    }

    public static void call(String signature) {
        if (DISABLED || BUSY.get()) return;
        BUSY.set(Boolean.TRUE);
        try {
            int d = depth();
            Deque<long[]> open = OPEN.get();
            closeStale(open, d, false);
            if (!open.isEmpty() && open.peek()[1] == d) {
                write("{\"ev\":\"call\",\"id\":" + open.peek()[0] + ",\"sig\":" + Graph.quote(signature) + "}");
            }
        } finally {
            BUSY.set(Boolean.FALSE);
        }
    }

    public static void ret(Object value, boolean isVoid) {
        if (DISABLED || BUSY.get()) return;
        BUSY.set(Boolean.TRUE);
        try {
            int d = depth();
            Deque<long[]> open = OPEN.get();
            closeStale(open, d, false);
            if (open.isEmpty() || open.peek()[1] != d) {
                return;
            }
            long id = open.pop()[0];
            ALL_OPEN.remove(id);
            StringBuilder sb = new StringBuilder(128);
            sb.append("{\"ev\":\"return\",\"id\":").append(id).append(",\"void\":").append(isVoid).append(",\"value\":");
            if (isVoid) sb.append("null"); else new Graph().value(sb, value, 0);
            sb.append('}');
            write(sb.toString());
        } catch (Throwable t) {
            write("{\"ev\":\"error\",\"where\":\"return\",\"msg\":" + Graph.quote(String.valueOf(t)) + "}");
        } finally {
            BUSY.set(Boolean.FALSE);
        }
    }

    /** An exception leaves the target (Kind.ERROR): close the innermost open invocation with its class. */
    public static void error(Throwable t) {
        if (DISABLED || BUSY.get()) return;
        BUSY.set(Boolean.TRUE);
        try {
            int d = depth();
            Deque<long[]> open = OPEN.get();
            closeStale(open, d, false);
            if (open.isEmpty() || open.peek()[1] != d) {
                return;
            }
            long id = open.pop()[0];
            ALL_OPEN.remove(id);
            String cls = t == null ? null : t.getClass().getName();
            write("{\"ev\":\"throw\",\"id\":" + id + ",\"exception\":" + (cls == null ? "null" : Graph.quote(cls)) + "}");
        } catch (Throwable e) {
            write("{\"ev\":\"error\",\"where\":\"throw\",\"msg\":" + Graph.quote(String.valueOf(e)) + "}");
        } finally {
            BUSY.set(Boolean.FALSE);
        }
    }

    /** Serialises one value as a field graph. */
    static final class Graph {
        private final IdentityHashMap<Object, Integer> seen = new IdentityHashMap<>();
        private int nodes;

        static String quote(String s) {
            StringBuilder b = new StringBuilder(s.length() + 2).append('"');
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '"': b.append("\\\""); break;
                    case '\\': b.append("\\\\"); break;
                    case '\n': b.append("\\n"); break;
                    case '\r': b.append("\\r"); break;
                    case '\t': b.append("\\t"); break;
                    default:
                        if (c < 0x20) b.append(String.format("\\u%04x", (int) c)); else b.append(c);
                }
            }
            return b.append('"').toString();
        }

        static boolean opaqueType(String name) {
            return name.contains("$MockitoMock$") || name.contains("$$EnhancerBySpringCGLIB$$")
                || name.contains("$$SpringCGLIB$$") || name.contains("$HibernateProxy$")
                || name.contains("$ByteBuddy$") || name.contains(".$Proxy") || name.startsWith("$Proxy")
                || name.contains("$$Lambda");
        }

        /**
         * Mockito inline mocks of concrete classes keep the class name of the mocked type, so the
         * name does not show them. Mockito is asked reflectively through the class loader of the
         * value (no compile-time dependency); any failure means "not a mock".
         */
        static boolean inlineMock(Object v) {
            ClassLoader cl = v.getClass().getClassLoader();
            if (cl == null) return false;
            try {
                Class<?> mockito = Class.forName("org.mockito.Mockito", false, cl);
                Object details = mockito.getMethod("mockingDetails", Object.class).invoke(null, v);
                Object isMock = details.getClass().getMethod("isMock").invoke(details);
                return Boolean.TRUE.equals(isMock);
            } catch (Throwable t) {
                return false;
            }
        }

        static boolean jdkType(String name) {
            return name.startsWith("java.") || name.startsWith("javax.") || name.startsWith("jdk.")
                || name.startsWith("sun.") || name.startsWith("com.sun.");
        }

        void value(StringBuilder sb, Object v, int depth) {
            if (v == null) { sb.append("null"); return; }
            Class<?> c = v.getClass();
            String type = c.getName();
            if (v instanceof String) { sb.append(quote((String) v)); return; }
            if (v instanceof Boolean || v instanceof Integer || v instanceof Long || v instanceof Short
                    || v instanceof Byte) { sb.append(v); return; }
            if (v instanceof Double || v instanceof Float) {
                double d = ((Number) v).doubleValue();
                if (Double.isNaN(d) || Double.isInfinite(d)) sb.append(quote(String.valueOf(d))); else sb.append(v);
                return;
            }
            if (v instanceof Class) {
                sb.append("{\"$t\":\"java.lang.Class\",\"$str\":").append(quote(((Class<?>) v).getName())).append('}');
                return;
            }
            if (v instanceof Character) { sb.append("{\"$t\":\"char\",\"v\":").append(quote(v.toString())).append('}'); return; }
            if (v instanceof Enum) {
                sb.append("{\"$t\":").append(quote(((Enum<?>) v).getDeclaringClass().getName()))
                  .append(",\"$enum\":").append(quote(((Enum<?>) v).name())).append('}');
                return;
            }
            if (opaqueType(type) || (!jdkType(type) && inlineMock(v))) {
                sb.append("{\"$t\":").append(quote(type)).append(",\"$opaque\":true}");
                return;
            }
            Integer ref = seen.get(v);
            if (ref != null) { sb.append("{\"$ref\":").append(ref).append('}'); return; }
            if (depth >= DEPTH || nodes >= BUDGET) {
                sb.append("{\"$t\":").append(quote(type)).append(",\"$cut\":true}");
                return;
            }
            int id = seen.size() + 1;
            seen.put(v, id);
            nodes++;
            sb.append("{\"$t\":").append(quote(type)).append(",\"$id\":").append(id);
            if (c.isArray()) {
                int n = Array.getLength(v);
                sb.append(",\"$len\":").append(n).append(",\"$items\":[");
                for (int i = 0; i < Math.min(n, MAX_ELEMENTS); i++) {
                    if (i > 0) sb.append(',');
                    value(sb, Array.get(v, i), depth + 1);
                }
                sb.append("]}");
                return;
            }
            if (jdkType(type)) {
                if (v instanceof Map) {
                    Map<?, ?> m = (Map<?, ?>) v;
                    sb.append(",\"$len\":").append(m.size()).append(",\"$entries\":[");
                    int i = 0;
                    for (Map.Entry<?, ?> e : m.entrySet()) {
                        if (i >= MAX_ELEMENTS) break;
                        if (i++ > 0) sb.append(',');
                        sb.append('[');
                        value(sb, e.getKey(), depth + 1);
                        sb.append(',');
                        value(sb, e.getValue(), depth + 1);
                        sb.append(']');
                    }
                    sb.append("]}");
                    return;
                }
                if (v instanceof Collection) {
                    Collection<?> col = (Collection<?>) v;
                    sb.append(",\"$len\":").append(col.size()).append(",\"$items\":[");
                    int i = 0;
                    for (Object o : col) {
                        if (i >= MAX_ELEMENTS) break;
                        if (i++ > 0) sb.append(',');
                        value(sb, o, depth + 1);
                    }
                    sb.append("]}");
                    return;
                }
                // other JDK values (time, BigDecimal, UUID, Optional, ...): textual form
                String s;
                try { s = String.valueOf(v); } catch (Throwable t) { s = "<toString failed>"; }
                sb.append(",\"$str\":").append(quote(s)).append('}');
                return;
            }
            sb.append(",\"$fields\":{");
            boolean first = true;
            for (Class<?> k = c; k != null && k != Object.class; k = k.getSuperclass()) {
                if (jdkType(k.getName())) break;
                for (Field f : k.getDeclaredFields()) {
                    if (Modifier.isStatic(f.getModifiers()) || f.isSynthetic()) continue;
                    Object fv;
                    try {
                        f.setAccessible(true);
                        fv = f.get(v);
                    } catch (Throwable t) {
                        continue;
                    }
                    if (!first) sb.append(',');
                    first = false;
                    String key = k == c ? f.getName() : k.getSimpleName() + "." + f.getName();
                    sb.append(quote(key)).append(':');
                    value(sb, fv, depth + 1);
                }
            }
            sb.append("}}");
        }
    }

    // keep for javac -Xlint
    @SuppressWarnings("unused")
    private static List<Object> unused() { return new ArrayList<>(); }
}
