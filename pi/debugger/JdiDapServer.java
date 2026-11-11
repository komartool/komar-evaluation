/*
 * JDI debug server of the autonomous mode (pi/debugger.ts talks to it).
 *
 * Derived from JdiDapServer.java of mcp-debugger (https://github.com/debugmcp/mcp-debugger,
 * packages/adapter-java/java/JdiDapServer.java at commit be84732, MIT License). Kept from it: the
 * Content-Length framing of the Debug Adapter Protocol, the JSON reader and writer and the expression
 * evaluator. Changed for this harness:
 *   - attach only (the harness starts the scenario JVM itself): no launch, no hot class redefinition,
 *     no stepping, no pause;
 *   - breakpoints on a source line, on the entry of a method and on its exit, including the exit by an
 *     exception; the exit stop carries the returned value or the exception;
 *   - one request per operation of the agent's tools (setBreakpoint, removeBreakpoint, resume,
 *     evaluate) and one `stopped` event with everything the agent sees at a stop (place, frames,
 *     locals of the top frame, returned value);
 *   - values are rendered by the server with depth and size limits; objects are referred to as @N
 *     (the JDI unique id), valid until the JVM run ends;
 *   - no code of the program runs: values are read from fields (JDK collections and maps are walked
 *     through their internal fields, JdkContainers); the evaluator allows a fixed list of calls
 *     (size, isEmpty, get, containsKey, contains on JDK containers with JDK keys, length/charAt on
 *     String, toString/equals on String and boxes, getClass().getName()) and answers them from
 *     fields too (JdkCalls); calls of project methods and static calls are refused. JDK methods run
 *     only for the text of a few immutable JDK value types (numbers, dates, URI, Locale).
 *
 * Usage: java JdiDapServer --port <port|0>   (0: an ephemeral port, printed as "LISTENING <port>")
 */
import com.sun.jdi.*;
import com.sun.jdi.connect.*;
import com.sun.jdi.event.*;
import com.sun.jdi.request.*;

import java.io.*;
import java.net.*;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.*;
import java.util.concurrent.atomic.*;

public class JdiDapServer {

    private final AtomicInteger seq = new AtomicInteger(1);
    private volatile OutputStream clientOut;
    private volatile boolean running = true;
    private static boolean debug = false;

    // --- the JVM of the current run ---
    private volatile VirtualMachine vm;
    private volatile boolean suspended;           // the JVM is suspended (at start or at a stop)
    private volatile ThreadReference stopThread;  // thread of the current stop (null at start)
    private volatile Map<String, Value> stopExtras = new HashMap<>(); // returned / exception of an exit stop
    private volatile boolean invoking;            // the evaluator or the renderer runs a method in the JVM
    private volatile CountDownLatch started = new CountDownLatch(1);

    // --- breakpoints: specs of the client and the JDI requests made for them ---
    private final Object lock = new Object();
    private final Map<Integer, Spec> specs = new LinkedHashMap<>();
    private final Map<Method, BreakpointRequest> entryRequests = new HashMap<>();
    private final Set<String> prepareFilters = new HashSet<>();
    private final Map<ThreadReference, Deque<Track>> tracks = new HashMap<>();
    private final Map<Long, ObjectReference> refs = new ConcurrentHashMap<>();

    static final String NO_THIS = "static method: no this";
    static final int STR_CAP = 2000, LOCAL_STR_CAP = 300, ITEMS = 20, FIELDS = 40, MAX_DEPTH = 3, NODES = 800;
    static final int FRAMES = 200;

    static final class Spec {
        final int id;
        final String kind;         // line | entry | exit
        final String className;    // line: top-level class of the file; entry/exit: declaring class
        final int line;
        final String method;       // entry/exit
        final List<String> argTypes; // entry/exit; null: any overload
        final String condition;
        final List<BreakpointRequest> lineRequests = new ArrayList<>();
        final Set<Method> methods = new LinkedHashSet<>();
        final List<String> notes = new ArrayList<>();

        Spec(Map<String, Object> a) {
            id = intVal(a, "id");
            kind = strOr(a, "kind", "line");
            className = str(a, "className");
            line = intVal(a, "line");
            method = str(a, "method");
            List<Object> types = list(a, "argTypes");
            List<String> t = null;
            if (types != null) {
                t = new ArrayList<>();
                for (Object o : types) t.add(String.valueOf(o));
            }
            argTypes = t;
            String c = str(a, "condition");
            condition = c == null || c.trim().isEmpty() ? null : c;
        }
    }

    /** What one event set stops for (several breakpoints may share a location). */
    static final class Stop {
        final String reason;       // breakpoint | entry | exit | exception
        final ThreadReference thread;
        final List<Integer> hit = new ArrayList<>();
        final List<String> errors = new ArrayList<>();
        Method method;             // exit/exception: the method left
        Value returned;            // exit
        ExceptionEvent thrown;     // exception
        int targetFrame = -1;      // exception: frame index of the method left

        Stop(String reason, ThreadReference thread) { this.reason = reason; this.thread = thread; }

        static Stop merge(Stop a, Stop b) {
            if (a == null || a.hit.isEmpty() && a.errors.isEmpty()) return b != null ? b : a;
            if (b != null) {
                for (Integer id : b.hit) if (!a.hit.contains(id)) a.hit.add(id);
                a.errors.addAll(b.errors);
            }
            return a;
        }
    }

    /** A running invocation of a method with an exit breakpoint: its thread and stack depth at entry. */
    static final class Track {
        final Method method;
        final int depth;
        final List<EventRequest> requests = new ArrayList<>();
        Track(Method method, int depth) { this.method = method; this.depth = depth; }
    }

    public static void main(String[] args) throws Exception {
        int port = -1;
        for (int i = 0; i < args.length; i++) {
            if ("--port".equals(args[i]) && i + 1 < args.length) {
                port = Integer.parseInt(args[++i]);
            } else if ("--debug".equals(args[i])) {
                debug = true;
            }
        }
        if (port < 0) {
            System.err.println("Usage: java JdiDapServer --port <port|0>");
            System.exit(1);
        }
        JdiDapServer server = new JdiDapServer();
        Runtime.getRuntime().addShutdownHook(new Thread(server::disposeVm, "jdi-cleanup"));
        server.run(port);
    }

    private void run(int port) throws Exception {
        try (ServerSocket serverSocket = new ServerSocket(port, 1, InetAddress.getLoopbackAddress())) {
            System.out.println("LISTENING " + serverSocket.getLocalPort());
            System.out.flush();
            Socket client = serverSocket.accept();
            try {
                InputStream in = client.getInputStream();
                clientOut = client.getOutputStream();
                while (running) {
                    Map<String, Object> msg = readDapMessage(in);
                    if (msg == null) break;
                    handleMessage(msg);
                }
            } finally {
                client.close();
            }
        } finally {
            disposeVm();
        }
    }

    // ========== DAP Transport ==========

    private Map<String, Object> readDapMessage(InputStream in) throws IOException {
        // Read Content-Length header
        String line;
        int contentLength = -1;
        while ((line = readLine(in)) != null) {
            if (line.isEmpty()) break; // empty line separates header from body
            if (line.startsWith("Content-Length:")) {
                contentLength = Integer.parseInt(line.substring(15).trim());
            }
        }
        if (contentLength < 0) return null;

        // Read body
        byte[] body = new byte[contentLength];
        int read = 0;
        while (read < contentLength) {
            int n = in.read(body, read, contentLength - read);
            if (n < 0) return null;
            read += n;
        }

        String json = new String(body, StandardCharsets.UTF_8);
        return parseJson(json);
    }

    private String readLine(InputStream in) throws IOException {
        StringBuilder sb = new StringBuilder();
        int c;
        while ((c = in.read()) >= 0) {
            if (c == '\r') {
                int next = in.read();
                if (next == '\n') break;
                sb.append((char) c);
                if (next >= 0) sb.append((char) next);
            } else if (c == '\n') {
                break;
            } else {
                sb.append((char) c);
            }
        }
        if (c < 0 && sb.length() == 0) return null;
        return sb.toString();
    }

    private synchronized void sendDapMessage(Map<String, Object> msg) {
        try {
            if (clientOut == null) return;
            String json = toJson(msg);
            byte[] bytes = json.getBytes(StandardCharsets.UTF_8);
            String header = "Content-Length: " + bytes.length + "\r\n\r\n";
            clientOut.write(header.getBytes(StandardCharsets.UTF_8));
            clientOut.write(bytes);
            clientOut.flush();
        } catch (IOException e) {
            log("Send error: " + e.getMessage());
        }
    }

    // ========== Requests ==========

    private void handleMessage(Map<String, Object> msg) {
        if (!"request".equals(str(msg, "type"))) return;
        String command = str(msg, "command");
        int reqSeq = intVal(msg, "seq");
        Map<String, Object> args = map(msg, "arguments");
        if (args == null) args = new HashMap<>();
        try {
            switch (command) {
                case "initialize": sendResponse(reqSeq, command, true, new HashMap<>()); break;
                case "attach": handleAttach(reqSeq, args); break;
                case "setBreakpoint": handleSetBreakpoint(reqSeq, args); break;
                case "removeBreakpoint": handleRemoveBreakpoint(reqSeq, args); break;
                case "resume": handleResume(reqSeq); break;
                case "evaluate": handleEvaluate(reqSeq, args); break;
                case "breakpoints": handleBreakpoints(reqSeq); break;
                case "terminate": disposeVm(); sendResponse(reqSeq, command, true, new HashMap<>()); break;
                case "disconnect":
                    disposeVm();
                    sendResponse(reqSeq, command, true, new HashMap<>());
                    running = false;
                    break;
                default: sendErrorResponse(reqSeq, command, "unsupported command: " + command);
            }
        } catch (Exception e) {
            String name = e.getClass() == RuntimeException.class ? "" : e.getClass().getSimpleName() + ": ";
            sendErrorResponse(reqSeq, command, name + e.getMessage());
        }
    }

    private void handleAttach(int reqSeq, Map<String, Object> args) throws Exception {
        disposeVm();
        AttachingConnector connector = null;
        for (Connector c : Bootstrap.virtualMachineManager().allConnectors()) {
            if (c instanceof AttachingConnector && c.name().equals("com.sun.jdi.SocketAttach")) {
                connector = (AttachingConnector) c;
            }
        }
        if (connector == null) throw new IllegalStateException("SocketAttach connector not found");
        Map<String, Connector.Argument> a = connector.defaultArguments();
        a.get("hostname").setValue(strOr(args, "host", "127.0.0.1"));
        a.get("port").setValue(String.valueOf(intVal(args, "port")));
        if (a.containsKey("timeout")) a.get("timeout").setValue("3000");
        VirtualMachine attached;
        try {
            attached = connector.attach(a);
        } catch (IOException e) {
            sendErrorResponse(reqSeq, "attach", "not listening: " + e.getMessage());
            return;
        }
        synchronized (lock) {
            vm = attached;
            suspended = true;
            stopThread = null;
            stopExtras = new HashMap<>();
            refs.clear();
            tracks.clear();
            entryRequests.clear();
            prepareFilters.clear();
            started = new CountDownLatch(1);
            for (Spec s : specs.values()) {
                s.lineRequests.clear();
                s.methods.clear();
                s.notes.clear();
            }
        }
        Thread loop = new Thread(this::eventLoop, "jdi-event-loop");
        loop.setDaemon(true);
        loop.start();
        // the JVM was started with suspend=y; wait until its start event is taken, so that a resume
        // of the client cannot overtake it
        started.await(3, TimeUnit.SECONDS);
        List<Map<String, Object>> statuses = new ArrayList<>();
        synchronized (lock) {
            for (Spec s : specs.values()) {
                registerPrepare(s);
                installOnLoaded(s);
                statuses.add(status(s));
            }
        }
        Map<String, Object> body = new HashMap<>();
        body.put("description", attached.description());
        body.put("breakpoints", statuses);
        sendResponse(reqSeq, "attach", true, body);
    }

    private void handleSetBreakpoint(int reqSeq, Map<String, Object> args) {
        Spec s = new Spec(args);
        if (s.className == null || s.className.isEmpty()) {
            sendErrorResponse(reqSeq, "setBreakpoint", "className is required");
            return;
        }
        Map<String, Object> st;
        synchronized (lock) {
            Spec old = specs.remove(s.id);
            if (old != null) uninstall(old);
            specs.put(s.id, s);
            if (vm != null) {
                registerPrepare(s);
                installOnLoaded(s);
            }
            st = status(s);
        }
        sendResponse(reqSeq, "setBreakpoint", true, st);
    }

    private void handleRemoveBreakpoint(int reqSeq, Map<String, Object> args) {
        int id = intVal(args, "id");
        boolean found;
        synchronized (lock) {
            Spec s = specs.remove(id);
            found = s != null;
            if (s != null) uninstall(s);
        }
        sendResponse(reqSeq, "removeBreakpoint", true, mapOf("removed", found));
    }

    private void handleBreakpoints(int reqSeq) {
        List<Map<String, Object>> statuses = new ArrayList<>();
        synchronized (lock) {
            for (Spec s : specs.values()) statuses.add(status(s));
        }
        sendResponse(reqSeq, "breakpoints", true, mapOf("breakpoints", statuses));
    }

    private void handleResume(int reqSeq) {
        VirtualMachine v = vm;
        if (v == null) {
            sendErrorResponse(reqSeq, "resume", "no JVM attached");
            return;
        }
        if (!suspended) {
            sendResponse(reqSeq, "resume", true, mapOf("alreadyRunning", true));
            return;
        }
        suspended = false;
        stopThread = null;
        stopExtras = new HashMap<>();
        try {
            v.resume();
        } catch (VMDisconnectedException e) { /* the terminated event follows */ }
        sendResponse(reqSeq, "resume", true, new HashMap<>());
    }

    private void handleEvaluate(int reqSeq, Map<String, Object> args) throws Exception {
        String expression = str(args, "expression");
        int frameIndex = intVal(args, "frame");
        int depth = Math.max(0, Math.min(MAX_DEPTH, intVal(args, "depth")));
        ThreadReference t = stopThread;
        if (vm == null || !suspended || t == null) {
            sendErrorResponse(reqSeq, "evaluate", "not stopped at a breakpoint");
            return;
        }
        List<StackFrame> frames = t.frames();
        if (frameIndex < 0 || frameIndex >= frames.size()) {
            sendErrorResponse(reqSeq, "evaluate", "no frame " + frameIndex + " (the stack has " + frames.size() + " frames)");
            return;
        }
        StackFrame sf = frames.get(frameIndex);
        Map<String, Object> body = new HashMap<>();
        Renderer r = new Renderer(t, STR_CAP);
        if ("this".equals(expression == null ? "" : expression.trim()) && sf.thisObject() == null) {
            body.put("note", NO_THIS);
        } else if ("locals".equals(expression == null ? "" : expression.trim())) {
            body.put("locals", r.locals(sf, depth, frameIndex == 0 ? stopExtras : Collections.emptyMap()));
        } else {
            Value value;
            invoking = true;
            try {
                value = new ExprEvaluator(expression, vm, t, sf, refs,
                        frameIndex == 0 ? stopExtras : Collections.emptyMap()).evaluate();
            } finally {
                invoking = false;
            }
            body.put("value", r.render(value, depth));
        }
        sendResponse(reqSeq, "evaluate", true, body);
    }

    // ========== Breakpoint requests ==========

    private void registerPrepare(Spec s) {
        String outer = s.className.contains("$") ? s.className.substring(0, s.className.indexOf('$')) : s.className;
        for (String filter : new String[]{outer, outer + "$*"}) {
            if (prepareFilters.add(filter)) {
                ClassPrepareRequest r = vm.eventRequestManager().createClassPrepareRequest();
                r.addClassFilter(filter);
                r.setSuspendPolicy(EventRequest.SUSPEND_ALL);
                r.enable();
            }
        }
    }

    private void installOnLoaded(Spec s) {
        String outer = s.className.contains("$") ? s.className.substring(0, s.className.indexOf('$')) : s.className;
        for (ReferenceType rt : vm.classesByName(outer)) install(s, rt);
        for (ReferenceType rt : vm.allClasses()) {
            if (rt.name().startsWith(outer + "$")) install(s, rt);
        }
    }

    private void installAll(ReferenceType rt) {
        synchronized (lock) {
            for (Spec s : specs.values()) install(s, rt);
        }
    }

    private void install(Spec s, ReferenceType rt) {
        String name = rt.name();
        if ("line".equals(s.kind)) {
            if (!(name.equals(s.className) || name.startsWith(s.className + "$"))) return;
            try {
                List<Location> locs = rt.locationsOfLine(s.line);
                if (locs.isEmpty()) {
                    String note = "no executable code at line " + s.line + " of " + s.className
                            + " (a line in a nested class is set when that class loads)";
                    if (name.equals(s.className) && s.lineRequests.isEmpty() && !s.notes.contains(note)) s.notes.add(note);
                    return;
                }
                for (BreakpointRequest existing : s.lineRequests) {
                    if (existing.location().equals(locs.get(0))) return;
                }
                BreakpointRequest r = vm.eventRequestManager().createBreakpointRequest(locs.get(0));
                r.putProperty("kind", "line");
                r.putProperty("spec", s.id);
                r.setSuspendPolicy(EventRequest.SUSPEND_ALL);
                r.enable();
                s.lineRequests.add(r);
            } catch (AbsentInformationException e) {
                s.notes.add("no line information in " + name + " (compiled without -g)");
            }
            return;
        }
        if (!name.equals(s.className)) return;
        List<Method> matching = new ArrayList<>();
        for (Method m : rt.methods()) {
            if (matches(s, m)) matching.add(m);
        }
        if (matching.isEmpty()) {
            for (Method m : rt.allMethods()) {
                if (matches(s, m)) {
                    matching.add(m);
                    s.notes.add("inherited from " + m.declaringType().name() + ": it stops for every class that runs it");
                    break;
                }
            }
        }
        for (Method m : matching) {
            if (m.location() == null) {
                s.notes.add(signature(m) + " has no code (abstract or native)");
                continue;
            }
            s.methods.add(m);
            if (!entryRequests.containsKey(m)) {
                BreakpointRequest r = vm.eventRequestManager().createBreakpointRequest(m.location());
                r.putProperty("kind", "entry");
                r.setSuspendPolicy(EventRequest.SUSPEND_ALL);
                r.enable();
                entryRequests.put(m, r);
            }
        }
        if (matching.isEmpty()) {
            List<String> names = new ArrayList<>();
            for (Method m : rt.methodsByName(s.method)) {
                if (!m.isSynthetic() && !m.isBridge()) names.add(signature(m));
            }
            s.notes.add(names.isEmpty() ? "no method " + s.method + " in " + name
                    : "no overload with these argument types; overloads: " + String.join(", ", names));
        }
    }

    private static boolean matches(Spec s, Method m) {
        if (!m.name().equals(s.method)) return false;
        // A covariant override compiles to a synthetic bridge with the same name and parameters: it is
        // never the method the agent means, and choosing it gives no stops in the real method.
        if (m.isSynthetic() || m.isBridge()) return false;
        if (s.argTypes == null) return true;
        List<String> actual = m.argumentTypeNames();
        if (actual.size() != s.argTypes.size()) return false;
        for (int i = 0; i < actual.size(); i++) {
            String want = s.argTypes.get(i).replaceAll("<.*>", "").replaceAll("\\s+", "").replace('$', '.');
            String got = actual.get(i).replace('$', '.');
            if (!(got.equals(want) || got.endsWith("." + want))) return false;
        }
        return true;
    }

    private void uninstall(Spec s) {
        if (vm == null) return;
        EventRequestManager erm = vm.eventRequestManager();
        try {
            for (BreakpointRequest r : s.lineRequests) erm.deleteEventRequest(r);
            for (Method m : s.methods) {
                boolean used = false;
                for (Spec o : specs.values()) used |= o.methods.contains(m);
                BreakpointRequest r = entryRequests.get(m);
                if (!used && r != null) {
                    erm.deleteEventRequest(r);
                    entryRequests.remove(m);
                }
            }
        } catch (VMDisconnectedException e) { /* run over */ }
        s.lineRequests.clear();
        s.methods.clear();
    }

    private Map<String, Object> status(Spec s) {
        Map<String, Object> st = new HashMap<>();
        st.put("id", s.id);
        boolean set = "line".equals(s.kind) ? !s.lineRequests.isEmpty() : !s.methods.isEmpty();
        st.put("status", vm == null ? "pending" : set ? "set" : "pending");
        List<String> where = new ArrayList<>();
        for (BreakpointRequest r : s.lineRequests) where.add(r.location().declaringType().name() + ":" + r.location().lineNumber());
        for (Method m : s.methods) where.add(signature(m));
        st.put("where", where);
        st.put("notes", new ArrayList<>(s.notes));
        if (vm != null && !set) {
            String outer = s.className.contains("$") ? s.className.substring(0, s.className.indexOf('$')) : s.className;
            st.put("classLoaded", !vm.classesByName(outer).isEmpty());
        }
        return st;
    }

    private List<Spec> specsFor(Method m, String kind) {
        List<Spec> out = new ArrayList<>();
        for (Spec s : specs.values()) {
            if (s.kind.equals(kind) && s.methods.contains(m)) out.add(s);
        }
        return out;
    }

    static String signature(Method m) {
        List<String> types = new ArrayList<>();
        for (String t : m.argumentTypeNames()) types.add(t);
        return m.declaringType().name() + "#" + m.name() + "(" + String.join(",", types) + ")";
    }

    // ========== Events ==========

    /**
     * The reader takes event sets off the JDI queue; the worker decides on them. Events raised while
     * the evaluator or the renderer runs a method in the JVM (`invoking`) are resumed by the reader at
     * once: an invocation from the thread that would have to resume them would never return.
     */
    private void eventLoop() {
        VirtualMachine v = vm;
        BlockingQueue<EventSet> work = new LinkedBlockingQueue<>();
        Thread worker = new Thread(() -> {
            try {
                while (vm == v) {
                    EventSet set = work.poll(500, TimeUnit.MILLISECONDS);
                    if (set == null) continue;
                    if (!decide(v, set)) return;
                }
            } catch (VMDisconnectedException e) {
                terminated(v);
            } catch (InterruptedException e) {
                // server shutting down
            } catch (Exception e) {
                log("event worker error: " + e);
                terminated(v);
            }
        }, "jdi-event-worker");
        worker.setDaemon(true);
        worker.start();
        try {
            EventQueue queue = v.eventQueue();
            while (running && vm == v) {
                EventSet set = queue.remove();
                if (invoking && !ended(set)) {
                    // a class prepared while a method runs for the evaluator: breakpoints are put
                    // into it by the worker before the next event (the worker may hold the lock now)
                    for (Event event : set) {
                        if (event instanceof ClassPrepareEvent) prepared.add(((ClassPrepareEvent) event).referenceType());
                    }
                    set.resume();
                    continue;
                }
                work.put(set);
            }
        } catch (VMDisconnectedException e) {
            work.clear();
            terminated(v);
        } catch (InterruptedException e) {
            // server shutting down
        }
    }

    private final Queue<ReferenceType> prepared = new ConcurrentLinkedQueue<>();

    private static boolean ended(EventSet set) {
        for (Event event : set) {
            if (event instanceof VMDeathEvent || event instanceof VMDisconnectEvent) return true;
        }
        return false;
    }

    /** Handle one event set; false when the run is over. */
    private boolean decide(VirtualMachine v, EventSet set) throws Exception {
        for (ReferenceType rt; (rt = prepared.poll()) != null; ) installAll(rt);
        boolean keep = false;
        Stop stop = null;
        for (Event event : set) {
            if (event instanceof VMStartEvent) {
                keep = true;
                started.countDown();
            } else if (event instanceof VMDeathEvent || event instanceof VMDisconnectEvent) {
                terminated(v);
                return false;
            } else if (event instanceof ClassPrepareEvent) {
                installAll(((ClassPrepareEvent) event).referenceType());
            } else if (event instanceof BreakpointEvent) {
                stop = Stop.merge(stop, onBreakpoint((BreakpointEvent) event));
            } else if (event instanceof MethodExitEvent) {
                stop = Stop.merge(stop, onMethodExit((MethodExitEvent) event));
            } else if (event instanceof ExceptionEvent) {
                stop = Stop.merge(stop, onException((ExceptionEvent) event));
            }
        }
        if (stop != null && !(stop.hit.isEmpty() && stop.errors.isEmpty())) {
            Map<String, Object> body = stopBody(stop);
            suspended = true;
            sendEvent("stopped", body);
        } else if (!keep) {
            set.resume();
        }
        return true;
    }

    private void terminated(VirtualMachine v) {
        if (vm != v) return;
        vm = null;
        suspended = false;
        stopThread = null;
        sendEvent("terminated", new HashMap<>());
    }

    private Stop onBreakpoint(BreakpointEvent e) throws Exception {
        BreakpointRequest req = (BreakpointRequest) e.request();
        ThreadReference t = e.thread();
        boolean line = "line".equals(req.getProperty("kind"));
        Stop stop = new Stop(line ? "breakpoint" : "entry", t);
        synchronized (lock) {
            if (line) {
                Spec s = specs.get((Integer) req.getProperty("spec"));
                if (s != null && condition(s, t, Collections.emptyMap(), stop.errors)) stop.hit.add(s.id);
            } else {
                Method m = e.location().method();
                stop.method = m;
                if (!specsFor(m, "exit").isEmpty()) track(t, m);
                for (Spec s : specsFor(m, "entry")) {
                    if (condition(s, t, Collections.emptyMap(), stop.errors)) stop.hit.add(s.id);
                }
            }
        }
        return stop;
    }

    private void track(ThreadReference t, Method m) throws IncompatibleThreadStateException {
        Track tr = new Track(m, t.frameCount());
        EventRequestManager erm = vm.eventRequestManager();
        MethodExitRequest exit = erm.createMethodExitRequest();
        exit.addThreadFilter(t);
        exit.addClassFilter(m.declaringType());
        exit.setSuspendPolicy(EventRequest.SUSPEND_ALL);
        exit.enable();
        ExceptionRequest thrown = erm.createExceptionRequest(null, true, true);
        thrown.addThreadFilter(t);
        thrown.setSuspendPolicy(EventRequest.SUSPEND_ALL);
        thrown.enable();
        tr.requests.add(exit);
        tr.requests.add(thrown);
        tracks.computeIfAbsent(t, k -> new ArrayDeque<>()).push(tr);
    }

    private void untrack(Track tr) {
        try {
            for (EventRequest r : tr.requests) vm.eventRequestManager().deleteEventRequest(r);
        } catch (VMDisconnectedException e) { /* run over */ }
    }

    private Stop onMethodExit(MethodExitEvent e) throws Exception {
        ThreadReference t = e.thread();
        synchronized (lock) {
            Deque<Track> d = tracks.get(t);
            if (d == null || d.isEmpty()) return null;
            Track top = d.peek();
            if (!top.method.equals(e.method()) || t.frameCount() != top.depth) return null;
            d.pop();
            untrack(top);
            Stop stop = new Stop("exit", t);
            stop.method = top.method;
            Value returned = e.returnValue();
            stop.returned = returned instanceof VoidValue ? null : returned;
            Map<String, Value> extras = new HashMap<>();
            if (!"void".equals(top.method.returnTypeName())) extras.put("returned", stop.returned);
            for (Spec s : specsFor(top.method, "exit")) {
                if (condition(s, t, extras, stop.errors)) stop.hit.add(s.id);
            }
            return stop;
        }
    }

    private Stop onException(ExceptionEvent e) throws Exception {
        ThreadReference t = e.thread();
        synchronized (lock) {
            Deque<Track> d = tracks.get(t);
            if (d == null || d.isEmpty()) return null;
            List<StackFrame> frames = t.frames();
            int n = frames.size();
            Location catchAt = e.catchLocation();
            int catchIndex = -1;
            if (catchAt != null) {
                for (int i = 0; i < n; i++) {
                    if (frames.get(i).location().method().equals(catchAt.method())) {
                        catchIndex = i;
                        break;
                    }
                }
            }
            Track escaped = null;
            int escapedIndex = -1;
            while (!d.isEmpty()) {
                Track tr = d.peek();
                int index = n - tr.depth;  // frame index of the tracked invocation
                if (catchIndex != -1 && catchIndex <= index) break;
                d.pop();
                untrack(tr);
                if (escaped == null) {
                    escaped = tr;
                    escapedIndex = index;
                }
            }
            if (escaped == null) return null;
            Stop stop = new Stop("exception", t);
            stop.method = escaped.method;
            stop.thrown = e;
            stop.targetFrame = escapedIndex;
            Map<String, Value> extras = new HashMap<>();
            extras.put("exception", e.exception());
            for (Spec s : specsFor(escaped.method, "exit")) {
                if (condition(s, t, extras, stop.errors)) stop.hit.add(s.id);
            }
            return stop;
        }
    }

    private boolean condition(Spec s, ThreadReference t, Map<String, Value> extras, List<String> errors) {
        if (s.condition == null) return true;
        invoking = true;
        try {
            Value v = new ExprEvaluator(s.condition, vm, t, t.frame(0), refs, extras).evaluate();
            if (v instanceof BooleanValue) return ((BooleanValue) v).value();
            errors.add("#" + s.id + ": the condition is not boolean");
            return true;
        } catch (Exception ex) {
            errors.add("#" + s.id + ": condition error: " + ex.getMessage());
            return true;
        } finally {
            invoking = false;
        }
    }

    private Map<String, Object> stopBody(Stop stop) throws Exception {
        ThreadReference t = stop.thread;
        stopThread = t;
        Map<String, Value> extras = new HashMap<>();
        Map<String, Object> body = new HashMap<>();
        body.put("reason", stop.reason);
        body.put("thread", t.name());
        body.put("breakpoints", stop.hit);
        body.put("conditionErrors", stop.errors);
        List<StackFrame> frames = t.frames();
        List<Map<String, Object>> fs = new ArrayList<>();
        for (int i = 0; i < Math.min(FRAMES, frames.size()); i++) {
            Location loc = frames.get(i).location();
            Map<String, Object> f = new HashMap<>();
            f.put("index", i);
            f.put("class", loc.declaringType().name());
            f.put("method", loc.method().name());
            f.put("line", loc.lineNumber());
            try {
                f.put("source", loc.sourcePath());
            } catch (AbsentInformationException e) {
                f.put("source", null);
            }
            fs.add(f);
        }
        body.put("frames", fs);
        body.put("totalFrames", frames.size());
        Renderer r = new Renderer(t, LOCAL_STR_CAP);
        if (stop.method != null) {
            body.put("method", signature(stop.method));
            body.put("void", "void".equals(stop.method.returnTypeName()));
        }
        if ("exit".equals(stop.reason) && !"void".equals(stop.method.returnTypeName())) {
            extras.put("returned", stop.returned);
            body.put("returned", r.render(stop.returned, 0));
        }
        if (stop.thrown != null) {
            ObjectReference ex = stop.thrown.exception();
            extras.put("exception", ex);
            body.put("exception", r.render(ex, 0));
            body.put("exceptionType", ex.referenceType().name());
            Location at = stop.thrown.location();
            body.put("thrownAt", at.declaringType().name() + "." + at.method().name() + ":" + at.lineNumber());
            body.put("targetFrame", stop.targetFrame);
            Field message = ex.referenceType().fieldByName("detailMessage");
            Value text = message != null ? ex.getValue(message) : null;
            if (text instanceof StringReference) body.put("message", ((StringReference) text).value());
        }
        body.put("locals", r.stopLocals(frames.get(0), extras));
        stopExtras = extras;
        return body;
    }

    // ========== Rendering ==========

    /** JSON nodes of values with depth and size limits; pi/debugger.ts formats them as text. */
    private final class Renderer {
        final ThreadReference thread;
        final int strCap;
        int nodes;

        Renderer(ThreadReference thread, int strCap) {
            this.thread = thread;
            this.strCap = strCap;
        }

        List<Map<String, Object>> locals(StackFrame sf, int depth, Map<String, Value> extras) {
            List<Map<String, Object>> out = new ArrayList<>();
            ObjectReference self = sf.thisObject();
            List<LocalVariable> vars = null;
            Map<LocalVariable, Value> values = null;
            try {
                vars = sf.visibleVariables();
                values = sf.getValues(vars);
            } catch (AbsentInformationException e) {
                vars = null;
            }
            if (self != null) out.add(named("this", render(self, depth)));
            if (vars == null) out.add(named("<no local variable information>", null));
            else for (LocalVariable lv : vars) out.add(named(lv.name(), render(values.get(lv), depth)));
            for (Map.Entry<String, Value> x : extras.entrySet()) out.add(named(x.getKey(), render(x.getValue(), depth)));
            return out;
        }

        /** Locals of a stop: fields of this one level deep, arguments to depth 1, other locals as Type@N. */
        List<Map<String, Object>> stopLocals(StackFrame sf, Map<String, Value> extras) {
            // all values are read before rendering: rendering may call JDK methods, which resumes the
            // thread and invalidates the frame
            List<Map<String, Object>> out = new ArrayList<>();
            ObjectReference self = sf.thisObject();
            List<LocalVariable> vars = null;
            Map<LocalVariable, Value> values = null;
            try {
                vars = sf.visibleVariables();
                values = sf.getValues(vars);
            } catch (AbsentInformationException e) {
                vars = null;
            }
            if (self != null) out.add(named("this", render(self, 1), 1));
            if (vars == null) {
                out.add(named("<no local variable information>", null));
            } else {
                for (LocalVariable lv : vars) {
                    int d = lv.isArgument() ? 1 : 0;
                    out.add(named(lv.name(), render(values.get(lv), d), d));
                }
            }
            for (Map.Entry<String, Value> x : extras.entrySet()) out.add(named(x.getKey(), render(x.getValue(), 0)));
            return out;
        }

        private Map<String, Object> named(String name, Map<String, Object> value, int depth) {
            Map<String, Object> m = named(name, value);
            m.put("depth", depth);
            return m;
        }

        private Map<String, Object> named(String name, Map<String, Object> value) {
            Map<String, Object> m = new HashMap<>();
            m.put("name", name);
            m.put("value", value);
            return m;
        }

        Map<String, Object> render(Value v, int depth) {
            nodes++;
            Map<String, Object> n = new HashMap<>();
            if (v == null) {
                n.put("t", "null");
                return n;
            }
            if (v instanceof VoidValue) {
                n.put("t", "void");
                return n;
            }
            if (v instanceof PrimitiveValue) {
                n.put("t", "prim");
                n.put("type", v.type().name());
                n.put("v", v instanceof CharValue ? String.valueOf(((CharValue) v).value()) : v.toString());
                return n;
            }
            ObjectReference o = (ObjectReference) v;
            try {
                if (o.isCollected()) {
                    n.put("t", "collected");
                    return n;
                }
            } catch (Exception e) { /* render what we can */ }
            pin(o);
            String type = o.referenceType().name();
            n.put("type", type);
            n.put("ref", o.uniqueID());
            if (o instanceof StringReference) {
                String s = ((StringReference) o).value();
                n.put("t", "str");
                n.put("len", s.length());
                n.put("v", s.length() > strCap ? s.substring(0, strCap) : s);
                return n;
            }
            Value boxed = boxedValue(o);
            if (boxed != null) {
                n.put("t", "box");
                n.put("v", boxed instanceof CharValue ? String.valueOf(((CharValue) boxed).value()) : boxed.toString());
                return n;
            }
            if (o.referenceType() instanceof ClassType && ((ClassType) o.referenceType()).isEnum()) {
                Field f = o.referenceType().fieldByName("name");
                Value name = f != null ? o.getValue(f) : null;
                n.put("t", "enum");
                n.put("v", name instanceof StringReference ? ((StringReference) name).value() : "?");
                return n;
            }
            if (o instanceof ClassObjectReference) {
                n.put("t", "val");
                n.put("v", ((ClassObjectReference) o).reflectedType().name());
                return n;
            }
            Value[] wrapped = wrappedValue(o, type);
            if (wrapped != null) {
                n.put("t", "wrap");
                n.put("value", render(wrapped[0], depth));
                return n;
            }
            String text = fieldText(o, type);
            if (text == null) text = safeToString(o, type);
            if (text != null) {
                n.put("t", "val");
                n.put("v", text.length() > strCap ? text.substring(0, strCap) : text);
                return n;
            }
            if (o instanceof ArrayReference) {
                ArrayReference arr = (ArrayReference) o;
                n.put("t", "arr");
                n.put("size", arr.length());
                if (depth > 0 || isSimpleArray(arr)) {
                    int shown = Math.min(ITEMS, arr.length());
                    List<Object> items = new ArrayList<>();
                    if (shown > 0) {
                        for (Value item : arr.getValues(0, shown)) items.add(render(item, Math.max(0, depth - 1)));
                    }
                    n.put("items", items);
                } else {
                    n.put("cut", true);
                }
                return n;
            }
            Boolean isMap = JdkContainers.kind(o);
            if (isMap != null) {
                return container(n, o, depth, isMap);
            }
            n.put("t", "obj");
            if (depth <= 0 || nodes > NODES || restricted(o.referenceType())) {
                n.put("cut", true);
                return n;
            }
            List<Map<String, Object>> fields = new ArrayList<>();
            int count = 0;
            for (Field f : o.referenceType().allFields()) {
                if (f.isStatic()) continue;
                count++;
                if (fields.size() < FIELDS) fields.add(named(f.name(), render(o.getValue(f), depth - 1)));
            }
            n.put("fields", fields);
            if (count > fields.size()) n.put("more", count - fields.size());
            return n;
        }

        /** A JDK collection or map read from its fields (JdkContainers): no method of the program runs. */
        private Map<String, Object> container(Map<String, Object> n, ObjectReference o, int depth, boolean isMap) {
            n.put("t", isMap ? "map" : "coll");
            JdkContainers.Content c = JdkContainers.read(o, ITEMS);
            n.put("size", c == null ? -1 : c.size);
            if (c == null || depth <= 0 || nodes > NODES) {
                n.put("cut", true);
                return n;
            }
            List<Object> items = new ArrayList<>();
            for (Value[] item : c.items) {
                if (isMap) {
                    Map<String, Object> kv = new HashMap<>();
                    kv.put("k", render(item[0], Math.max(0, depth - 1)));
                    kv.put("v", render(item[1], Math.max(0, depth - 1)));
                    items.add(kv);
                } else {
                    items.add(render(item[0], Math.max(0, depth - 1)));
                }
            }
            n.put("items", items);
            return n;
        }

        private boolean isSimpleArray(ArrayReference arr) {
            String component = ((ArrayType) arr.referenceType()).componentTypeName();
            return isPrimitive(component) || "java.lang.String".equals(component);
        }

        /** A JDK method without arguments, run by the renderer itself (never a project method). */
        private Value call(ObjectReference o, String name) {
            try {
                Method m = null;
                for (Method c : ((ClassType) o.referenceType()).allMethods()) {
                    if (c.name().equals(name) && c.argumentTypeNames().isEmpty() && !c.isAbstract()) {
                        m = c;
                        break;
                    }
                }
                if (m == null || !m.declaringType().name().startsWith("java.")) return null;
                invoking = true;
                try {
                    return o.invokeMethod(thread, m, Collections.emptyList(), ObjectReference.INVOKE_SINGLE_THREADED);
                } finally {
                    invoking = false;
                }
            } catch (Exception e) {
                return null;
            }
        }

        /** The text of a file or a pattern: their path and pattern fields. */
        private String fieldText(ObjectReference o, String type) {
            String field = type.equals("java.io.File") ? "path" : type.equals("java.util.regex.Pattern") ? "pattern" : null;
            if (field == null) return null;
            Field f = o.referenceType().fieldByName(field);
            Value v = f == null ? null : o.getValue(f);
            return v instanceof StringReference ? ((StringReference) v).value() : null;
        }

        /** The value of a JDK holder (atomic references and numbers, Optional), read from its field. */
        private Value[] wrappedValue(ObjectReference o, String type) {
            String field;
            switch (type) {
                case "java.util.concurrent.atomic.AtomicReference": case "java.util.concurrent.atomic.AtomicInteger":
                case "java.util.concurrent.atomic.AtomicLong": case "java.util.concurrent.atomic.AtomicBoolean":
                case "java.util.Optional": case "java.util.OptionalInt": case "java.util.OptionalLong":
                case "java.util.OptionalDouble":
                    field = "value";
                    break;
                default:
                    return null;
            }
            Field f = o.referenceType().fieldByName(field);
            if (f == null) return null;
            Value v = o.getValue(f);
            if (type.equals("java.util.concurrent.atomic.AtomicBoolean") && v instanceof IntegerValue) {
                return new Value[] {o.virtualMachine().mirrorOf(((IntegerValue) v).value() != 0)};
            }
            return new Value[] {v};
        }

        /** toString of a JDK value class whose text depends on its own JDK fields only (never on objects of
         *  the program): numbers, dates and times, identifiers, character buffers. */
        private String safeToString(ObjectReference o, String type) {
            boolean safe = type.equals("java.math.BigDecimal") || type.equals("java.math.BigInteger")
                    || type.startsWith("java.time.") && !type.startsWith("java.time.chrono.") && !type.startsWith("java.time.format.")
                    && !type.startsWith("java.time.temporal.") && !type.equals("java.time.Clock")
                    || type.equals("java.util.UUID")
                    || type.equals("java.net.URI") || type.equals("java.lang.StringBuilder")
                    || type.equals("java.lang.StringBuffer")
                    || type.equals("sun.nio.fs.UnixPath") || type.equals("sun.nio.fs.WindowsPath")
                    || type.equals("java.util.Locale") || type.startsWith("sun.nio.cs.");
            if (!safe || !o.referenceType().name().equals(type)) return null;
            Value s = call(o, "toString");
            return s instanceof StringReference ? ((StringReference) s).value() : null;
        }
    }

    /** JDK collections and maps read from their fields: the debugger shows and searches them without
     *  calling a method, so no code of the program (hashCode, equals, compareTo, toString) runs. */
    static final class JdkContainers {
        static final int WALK = 1_000_000;

        static final class Content {
            int size;
            final List<Value[]> items = new ArrayList<>();
        }

        private static final class Acc {
            final int keep, stopAt;
            boolean keysOnly;
            int seen;
            final Content c = new Content();
            Acc(int keep, int stopAt) { this.keep = keep; this.stopAt = stopAt; }
            /** false: stop walking */
            boolean add(Value k, Value v, boolean map) {
                if (seen < keep) c.items.add(map && !keysOnly ? new Value[] {k, v} : new Value[] {k});
                seen++;
                return seen < stopAt;
            }
        }

        private static boolean wrapper(String t) {
            return t.startsWith("java.util.Collections$Unmodifiable") || t.startsWith("java.util.Collections$Synchronized")
                    || t.startsWith("java.util.Collections$Checked");
        }

        private static ObjectReference inner(ObjectReference o) {
            ObjectReference m = ref(o, "m");
            return m != null ? m : ref(o, "c");
        }

        /** TRUE a map, FALSE a collection, null: not a JDK container these methods can read. */
        static Boolean kind(ObjectReference o) {
            return kind(o, 0);
        }

        private static Boolean kind(ObjectReference o, int nest) {
            if (o == null || o instanceof ArrayReference || o instanceof StringReference || nest > 8) return null;
            String t = o.referenceType().name();
            switch (t) {
                case "java.util.HashMap": case "java.util.LinkedHashMap": case "java.util.TreeMap":
                case "java.util.Hashtable": case "java.util.Properties": case "java.util.IdentityHashMap":
                case "java.util.concurrent.ConcurrentHashMap": case "java.util.ImmutableCollections$Map1":
                case "java.util.ImmutableCollections$MapN": case "java.util.Collections$EmptyMap":
                case "java.util.Collections$SingletonMap":
                    return Boolean.TRUE;
                case "java.util.HashSet": case "java.util.LinkedHashSet":
                    return kind(ref(o, "map"), nest + 1) == Boolean.TRUE ? Boolean.FALSE : null;
                case "java.util.TreeSet":
                    return kind(ref(o, "m"), nest + 1) == Boolean.TRUE ? Boolean.FALSE : null;
                case "java.util.ArrayList": case "java.util.LinkedList": case "java.util.Vector": case "java.util.Stack":
                case "java.util.ArrayDeque": case "java.util.Arrays$ArrayList":
                case "java.util.concurrent.CopyOnWriteArrayList":
                case "java.util.ImmutableCollections$List12": case "java.util.ImmutableCollections$ListN":
                case "java.util.ImmutableCollections$Set12": case "java.util.ImmutableCollections$SetN":
                case "java.util.Collections$EmptyList": case "java.util.Collections$EmptySet":
                case "java.util.Collections$SingletonList": case "java.util.Collections$SingletonSet":
                    return Boolean.FALSE;
                default:
                    return wrapper(t) ? kind(inner(o), nest + 1) : null;
            }
        }

        static boolean isList(ObjectReference o) {
            String t = o.referenceType().name();
            if (wrapper(t)) return t.contains("List") && isList(inner(o));
            switch (t) {
                case "java.util.ArrayList": case "java.util.LinkedList": case "java.util.Vector": case "java.util.Stack":
                case "java.util.Arrays$ArrayList": case "java.util.concurrent.CopyOnWriteArrayList":
                case "java.util.ImmutableCollections$List12": case "java.util.ImmutableCollections$ListN":
                case "java.util.Collections$EmptyList": case "java.util.Collections$SingletonList":
                    return true;
                default:
                    return false;
            }
        }

        /** Whether equality of keys decides membership: not a sorted map or set with a comparator. */
        static boolean byEquality(ObjectReference o) {
            String t = o.referenceType().name();
            if (wrapper(t)) return byEquality(inner(o));
            if (t.equals("java.util.TreeSet")) return byEquality(ref(o, "m"));
            if (t.equals("java.util.TreeMap")) return ref(o, "comparator") == null;
            return true;
        }

        /** The size and the first `keep` items; null if the object cannot be read. */
        static Content read(ObjectReference o, int keep) {
            int known;
            try {
                known = knownSize(o, 0);
            } catch (RuntimeException e) {
                known = -1;
            }
            Acc a = new Acc(keep, known >= 0 ? Math.max(1, keep) : WALK);
            try {
                if (!walk(o, a, 0)) return null;
            } catch (RuntimeException e) {
                return null;
            }
            a.c.size = known >= 0 ? known : a.seen;
            return a.c;
        }

        /** All items (up to WALK). */
        static Content readAll(ObjectReference o) {
            Acc a = new Acc(WALK, WALK);
            try {
                if (!walk(o, a, 0)) return null;
            } catch (RuntimeException e) {
                return null;
            }
            a.c.size = a.seen;
            return a.c;
        }

        private static int knownSize(ObjectReference o, int nest) {
            if (o == null || nest > 8) return -1;
            String t = o.referenceType().name();
            switch (t) {
                case "java.util.ArrayList": case "java.util.LinkedList": case "java.util.HashMap":
                case "java.util.LinkedHashMap": case "java.util.TreeMap": case "java.util.IdentityHashMap":
                case "java.util.ImmutableCollections$MapN": case "java.util.ImmutableCollections$SetN":
                    return num(o, "size");
                case "java.util.Vector": case "java.util.Stack":
                    return num(o, "elementCount");
                case "java.util.Hashtable":
                    return num(o, "count");
                case "java.util.HashSet": case "java.util.LinkedHashSet":
                    return knownSize(ref(o, "map"), nest + 1);
                case "java.util.TreeSet":
                    return knownSize(ref(o, "m"), nest + 1);
                case "java.util.Arrays$ArrayList": {
                    ArrayReference a = arr(o, "a");
                    return a == null ? -1 : a.length();
                }
                case "java.util.concurrent.CopyOnWriteArrayList": {
                    ArrayReference a = arr(o, "array");
                    return a == null ? -1 : a.length();
                }
                case "java.util.ImmutableCollections$ListN": {
                    ArrayReference a = arr(o, "elements");
                    return a == null ? -1 : a.length();
                }
                case "java.util.Collections$EmptyList": case "java.util.Collections$EmptySet":
                case "java.util.Collections$EmptyMap":
                    return 0;
                case "java.util.Collections$SingletonList": case "java.util.Collections$SingletonSet":
                case "java.util.Collections$SingletonMap": case "java.util.ImmutableCollections$Map1":
                    return 1;
                default:
                    return wrapper(t) ? knownSize(inner(o), nest + 1) : -1;
            }
        }

        private static boolean walk(ObjectReference o, Acc a, int nest) {
            if (o == null || nest > 8) return false;
            String t = o.referenceType().name();
            switch (t) {
                case "java.util.ArrayList":
                    return array(arr(o, "elementData"), num(o, "size"), a);
                case "java.util.Vector": case "java.util.Stack":
                    return array(arr(o, "elementData"), num(o, "elementCount"), a);
                case "java.util.Arrays$ArrayList":
                    return array(arr(o, "a"), -1, a);
                case "java.util.concurrent.CopyOnWriteArrayList":
                    return array(arr(o, "array"), -1, a);
                case "java.util.ImmutableCollections$ListN":
                    return array(arr(o, "elements"), -1, a);
                case "java.util.ImmutableCollections$SetN": {
                    ArrayReference es = arr(o, "elements");
                    if (es == null) return false;
                    for (Value v : es.getValues()) if (v != null && !a.add(v, null, false)) break;
                    return true;
                }
                case "java.util.ImmutableCollections$List12": case "java.util.ImmutableCollections$Set12": {
                    Value e0 = val(o, "e0"), e1 = val(o, "e1");
                    if (!a.add(e0, null, false)) return true;
                    if (e1 != null && !isEmptyMarker(o.virtualMachine(), e1)) a.add(e1, null, false);
                    return true;
                }
                case "java.util.ImmutableCollections$Map1":
                    a.add(val(o, "k0"), val(o, "v0"), true);
                    return true;
                case "java.util.ImmutableCollections$MapN": {
                    ArrayReference tab = arr(o, "table");
                    if (tab == null) return false;
                    List<Value> vs = tab.getValues();
                    for (int i = 0; i + 1 < vs.size(); i += 2) {
                        if (vs.get(i) != null && !a.add(vs.get(i), vs.get(i + 1), true)) break;
                    }
                    return true;
                }
                case "java.util.Collections$EmptyList": case "java.util.Collections$EmptySet":
                case "java.util.Collections$EmptyMap":
                    return true;
                case "java.util.Collections$SingletonList": case "java.util.Collections$SingletonSet":
                    a.add(val(o, "element"), null, false);
                    return true;
                case "java.util.Collections$SingletonMap":
                    a.add(val(o, "k"), val(o, "v"), true);
                    return true;
                case "java.util.LinkedList": {
                    ObjectReference n = ref(o, "first");
                    for (int guard = 0; n != null && guard < WALK; guard++) {
                        if (!a.add(val(n, "item"), null, false)) break;
                        n = ref(n, "next");
                    }
                    return true;
                }
                case "java.util.ArrayDeque": {
                    ArrayReference es = arr(o, "elements");
                    if (es == null) return false;
                    int len = es.length(), head = num(o, "head"), tail = num(o, "tail");
                    for (int i = head, guard = 0; len > 0 && i != tail && guard < len; i = (i + 1) % len, guard++) {
                        if (!a.add(es.getValue(i), null, false)) break;
                    }
                    return true;
                }
                case "java.util.LinkedHashMap": {
                    ObjectReference e = ref(o, "head");
                    for (int guard = 0; e != null && guard < WALK; guard++) {
                        if (!a.add(val(e, "key"), val(e, "value"), true)) break;
                        e = ref(e, "after");
                    }
                    return true;
                }
                case "java.util.HashMap": case "java.util.Hashtable":
                    return table(arr(o, "table"), "value", a);
                case "java.util.Properties": {
                    ObjectReference m = ref(o, "map");
                    return m != null ? walk(m, a, nest + 1) : table(arr(o, "table"), "value", a);
                }
                case "java.util.concurrent.ConcurrentHashMap":
                    return table(arr(o, "table"), "val", a);
                case "java.util.IdentityHashMap": {
                    ArrayReference tab = arr(o, "table");
                    if (tab == null) return false;
                    List<Value> vs = tab.getValues();
                    for (int i = 0; i + 1 < vs.size(); i += 2) {
                        if (vs.get(i) != null && !a.add(vs.get(i), vs.get(i + 1), true)) break;
                    }
                    return true;
                }
                case "java.util.TreeMap":
                    inorder(ref(o, "root"), a);
                    return true;
                case "java.util.HashSet": case "java.util.LinkedHashSet": case "java.util.TreeSet": {
                    ObjectReference m = ref(o, t.equals("java.util.TreeSet") ? "m" : "map");
                    boolean before = a.keysOnly;
                    a.keysOnly = true;
                    try {
                        return walk(m, a, nest + 1);
                    } finally {
                        a.keysOnly = before;
                    }
                }
                default:
                    return wrapper(t) && walk(inner(o), a, nest + 1);
            }
        }

        /** Buckets of a hash table: chains of nodes with key, value and next (a tree bin of a concurrent
         *  map through its list of nodes; forwarding and reservation nodes carry no entry). */
        private static boolean table(ArrayReference tab, String value, Acc a) {
            if (tab == null) return true;  // no table yet: empty
            for (Value b : tab.getValues()) {
                ObjectReference n = (ObjectReference) b;
                if (n != null && n.referenceType().name().endsWith("$TreeBin")) n = ref(n, "first");
                for (int guard = 0; n != null && guard < WALK; guard++) {
                    String nt = n.referenceType().name();
                    if (nt.endsWith("$ForwardingNode") || nt.endsWith("$ReservationNode")) break;
                    if (!a.add(val(n, "key"), val(n, value), true)) return true;
                    n = ref(n, "next");
                }
            }
            return true;
        }

        private static void inorder(ObjectReference root, Acc a) {
            Deque<ObjectReference> stack = new ArrayDeque<>();
            ObjectReference n = root;
            int guard = 0;
            while ((n != null || !stack.isEmpty()) && guard++ < WALK) {
                while (n != null) {
                    stack.push(n);
                    n = ref(n, "left");
                }
                n = stack.pop();
                if (!a.add(val(n, "key"), val(n, "value"), true)) return;
                n = ref(n, "right");
            }
        }

        private static boolean array(ArrayReference arr, int size, Acc a) {
            if (arr == null) return size <= 0;
            int n = size < 0 ? arr.length() : Math.min(size, arr.length());
            for (int i = 0; i < n; i += 256) {
                for (Value v : arr.getValues(i, Math.min(256, n - i))) {
                    if (!a.add(v, null, false)) return true;
                }
            }
            return true;
        }

        /** ImmutableCollections.EMPTY: the second element of a one-element List12 or Set12 (JDK 17+). */
        private static boolean isEmptyMarker(VirtualMachine vm, Value v) {
            for (ReferenceType t : vm.classesByName("java.util.ImmutableCollections")) {
                Field f = t.fieldByName("EMPTY");
                if (f != null && f.isStatic()) return v.equals(t.getValue(f));
            }
            return false;
        }

        static Value val(ObjectReference o, String name) {
            Field f = o.referenceType().fieldByName(name);
            if (f == null) throw new IllegalStateException("no field " + name + " in " + o.referenceType().name());
            return o.getValue(f);
        }

        static ObjectReference ref(ObjectReference o, String name) {
            if (o == null) return null;
            Field f = o.referenceType().fieldByName(name);
            if (f == null) return null;
            Value v = o.getValue(f);
            return v instanceof ObjectReference ? (ObjectReference) v : null;
        }

        private static ArrayReference arr(ObjectReference o, String name) {
            ObjectReference r = ref(o, name);
            return r instanceof ArrayReference ? (ArrayReference) r : null;
        }

        private static int num(ObjectReference o, String name) {
            Value v = val(o, name);
            return v instanceof IntegerValue ? ((IntegerValue) v).value() : -1;
        }
    }

    /** The calls the evaluator allows (ExprEvaluator.allowedCall), answered from fields and mirrors. */
    static final class JdkCalls {
        static Value answer(VirtualMachine vm, ObjectReference o, String name, List<Value> args) {
            if (o instanceof StringReference) {
                String s = ((StringReference) o).value();
                switch (name) {
                    case "length": return vm.mirrorOf(s.length());
                    case "isEmpty": return vm.mirrorOf(s.isEmpty());
                    case "toString": return o;
                    case "equals": return vm.mirrorOf(jdkEquals(o, args.get(0)));
                    case "charAt": {
                        int i = index(args.get(0));
                        if (i < 0 || i >= s.length()) throw new IllegalArgumentException("charAt(" + i + "): index out of bounds for length " + s.length());
                        return vm.mirrorOf(s.charAt(i));
                    }
                    default: break;
                }
            }
            Value boxed = boxedValue(o);
            if (boxed != null) {
                if (name.equals("toString")) {
                    return vm.mirrorOf(boxed instanceof CharValue ? String.valueOf(((CharValue) boxed).value()) : boxed.toString());
                }
                if (name.equals("equals")) return vm.mirrorOf(jdkEquals(o, args.get(0)));
            }
            if (name.equals("getClass")) return o.referenceType().classObject();
            if (o instanceof ClassObjectReference) {
                String n = ((ClassObjectReference) o).reflectedType().name();
                if (name.equals("getName")) return vm.mirrorOf(n);
                if (name.equals("getSimpleName")) {
                    String simple = n.substring(n.lastIndexOf('.') + 1);
                    simple = simple.substring(simple.lastIndexOf('$') + 1);
                    return vm.mirrorOf(simple.matches("\\d+") ? "" : simple);
                }
            }
            Boolean map = JdkContainers.kind(o);
            if (map != null) {
                if (name.equals("size") || name.equals("isEmpty")) {
                    JdkContainers.Content c = JdkContainers.read(o, 0);
                    if (c == null) throw new IllegalArgumentException("cannot read " + o.referenceType().name());
                    return name.equals("size") ? vm.mirrorOf(c.size) : vm.mirrorOf(c.size == 0);
                }
                if (name.equals("get") && !map) {
                    int i = index(args.get(0));
                    JdkContainers.Content c = JdkContainers.read(o, Math.max(0, i) + 1);
                    if (c == null) throw new IllegalArgumentException("cannot read " + o.referenceType().name());
                    if (i < 0 || i >= c.size) throw new IllegalArgumentException("get(" + i + "): index out of bounds for size " + c.size);
                    return c.items.get(i)[0];
                }
                JdkContainers.Content c = JdkContainers.readAll(o);
                if (c == null) throw new IllegalArgumentException("cannot read " + o.referenceType().name());
                for (Value[] item : c.items) {
                    if (jdkEquals(args.get(0), item[0])) {
                        return name.equals("get") ? item[1] : vm.mirrorOf(true);
                    }
                }
                return name.equals("get") ? null : vm.mirrorOf(false);
            }
            throw new IllegalArgumentException("no such call: " + name);
        }

        private static int index(Value v) {
            Value u = v instanceof ObjectReference ? boxedValue((ObjectReference) v) : v;
            if (u instanceof IntegerValue) return ((IntegerValue) u).value();
            if (u instanceof ShortValue) return ((ShortValue) u).value();
            if (u instanceof ByteValue) return ((ByteValue) u).value();
            if (u instanceof CharValue) return ((CharValue) u).value();
            throw new IllegalArgumentException("an int index is needed");
        }

        /** a.equals(b) for a string, a boxed value, a primitive (boxed by Java) or an enum constant. */
        static boolean jdkEquals(Value a, Value b) {
            if (a == null || b == null) return a == b;
            Value pa = a instanceof PrimitiveValue ? a : a instanceof ObjectReference ? boxedValue((ObjectReference) a) : null;
            if (a instanceof StringReference) {
                return b instanceof StringReference && ((StringReference) a).value().equals(((StringReference) b).value());
            }
            if (pa != null) {
                Value pb = b instanceof ObjectReference ? boxedValue((ObjectReference) b) : null;
                return pb != null && pa.equals(pb);
            }
            return a.equals(b);  // an enum constant: the same object
        }
    }

    static final String RESTRICTED = "the fields of %s are not shown: class objects, class loaders, threads, "
            + "the system, reflection and JDK internals are not part of the state of the program";

    /** Objects whose fields the debugger neither shows nor reads: they lead to the class path, the file
     *  system or the environment, not to the state of the program. */
    static boolean restricted(ReferenceType t) {
        String n = t.name();
        if (n.equals("java.lang.Class") || n.equals("java.lang.System") || n.equals("java.lang.Runtime")
                || n.equals("java.lang.Module") || n.equals("java.lang.ModuleLayer") || n.equals("java.lang.Package")
                || n.startsWith("java.lang.Process") || n.startsWith("java.lang.reflect.") || n.startsWith("java.lang.invoke.")
                || n.startsWith("java.security.") || n.startsWith("jdk.internal.") || n.startsWith("sun.")
                || n.startsWith("com.sun.") || n.startsWith("java.lang.ref.") || n.startsWith("java.lang.Thread")) {
            return true;
        }
        if (t instanceof ClassType) {
            for (ClassType c = (ClassType) t; c != null; c = c.superclass()) {
                String s = c.name();
                if (s.equals("java.lang.ClassLoader") || s.equals("java.lang.Thread") || s.equals("java.lang.ThreadGroup")) {
                    return true;
                }
            }
        }
        return false;
    }

    static final int PINNED = 10_000;

    /** Remember an object shown to the agent as @N and keep it from being collected until the JVM run
     *  ends (at most PINNED objects; later ones may show as <collected>). */
    private void pin(ObjectReference o) {
        if (refs.putIfAbsent(o.uniqueID(), o) == null && refs.size() <= PINNED) {
            try {
                o.disableCollection();
            } catch (ObjectCollectedException | VMDisconnectedException | UnsupportedOperationException e) {
                // gone already, or the JVM ended: nothing to pin
            }
        }
    }

    private static Value boxedValue(ObjectReference o) {
        switch (o.referenceType().name()) {
            case "java.lang.Integer": case "java.lang.Long": case "java.lang.Short": case "java.lang.Byte":
            case "java.lang.Character": case "java.lang.Boolean": case "java.lang.Float": case "java.lang.Double":
                Field f = o.referenceType().fieldByName("value");
                return f != null ? o.getValue(f) : null;
            default:
                return null;
        }
    }

    private static boolean implementsType(ReferenceType t, String iface) {
        if (t instanceof ClassType) {
            for (InterfaceType i : ((ClassType) t).allInterfaces()) {
                if (i.name().equals(iface)) return true;
            }
        }
        return false;
    }

    private static boolean isPrimitive(String name) {
        switch (name) {
            case "int": case "long": case "short": case "byte": case "char": case "boolean": case "float": case "double":
                return true;
            default:
                return false;
        }
    }

    private synchronized void disposeVm() {
        VirtualMachine v = vm;
        vm = null;
        suspended = false;
        stopThread = null;
        if (v != null) {
            try {
                v.exit(1);
            } catch (Exception e) { /* already gone */ }
            try {
                v.dispose();
            } catch (Exception e) { /* already gone */ }
        }
    }

    // ========== DAP messages ==========

    private void sendResponse(int reqSeq, String command, boolean success, Map<String, Object> body) {
        Map<String, Object> resp = new HashMap<>();
        resp.put("seq", seq.getAndIncrement());
        resp.put("type", "response");
        resp.put("request_seq", reqSeq);
        resp.put("success", success);
        resp.put("command", command);
        resp.put("body", body);
        sendDapMessage(resp);
    }

    private void sendErrorResponse(int reqSeq, String command, String message) {
        Map<String, Object> resp = new HashMap<>();
        resp.put("seq", seq.getAndIncrement());
        resp.put("type", "response");
        resp.put("request_seq", reqSeq);
        resp.put("success", false);
        resp.put("command", command);
        resp.put("message", message);
        resp.put("body", new HashMap<>());
        sendDapMessage(resp);
    }

    private void sendEvent(String event, Map<String, Object> body) {
        Map<String, Object> evt = new HashMap<>();
        evt.put("seq", seq.getAndIncrement());
        evt.put("type", "event");
        evt.put("event", event);
        evt.put("body", body);
        sendDapMessage(evt);
    }

    // ========== Minimal JSON Parser/Writer (no deps) ==========

    @SuppressWarnings("unchecked")
    private Map<String, Object> parseJson(String json) {
        return (Map<String, Object>) new JsonParser(json).parseValue();
    }

    private String toJson(Object obj) {
        StringBuilder sb = new StringBuilder();
        writeJson(sb, obj);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private void writeJson(StringBuilder sb, Object obj) {
        if (obj == null) {
            sb.append("null");
        } else if (obj instanceof Boolean) {
            sb.append(obj);
        } else if (obj instanceof Number) {
            // Avoid scientific notation for integers
            if (obj instanceof Double || obj instanceof Float) {
                double d = ((Number) obj).doubleValue();
                if (d == Math.floor(d) && !Double.isInfinite(d)) {
                    sb.append((long) d);
                } else {
                    sb.append(d);
                }
            } else {
                sb.append(obj);
            }
        } else if (obj instanceof String) {
            sb.append('"');
            String s = (String) obj;
            for (int i = 0; i < s.length(); i++) {
                char c = s.charAt(i);
                switch (c) {
                    case '"': sb.append("\\\""); break;
                    case '\\': sb.append("\\\\"); break;
                    case '\n': sb.append("\\n"); break;
                    case '\r': sb.append("\\r"); break;
                    case '\t': sb.append("\\t"); break;
                    default:
                        if (c < 0x20) {
                            sb.append(String.format("\\u%04x", (int) c));
                        } else {
                            sb.append(c);
                        }
                }
            }
            sb.append('"');
        } else if (obj instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> entry : ((Map<?, ?>) obj).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeJson(sb, String.valueOf(entry.getKey()));
                sb.append(':');
                writeJson(sb, entry.getValue());
            }
            sb.append('}');
        } else if (obj instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object item : (List<?>) obj) {
                if (!first) sb.append(',');
                first = false;
                writeJson(sb, item);
            }
            sb.append(']');
        } else {
            sb.append('"');
            sb.append(obj.toString().replace("\"", "\\\""));
            sb.append('"');
        }
    }

    // ========== Expression Evaluator ==========

    /**
     * Recursive descent expression evaluator using JDI.
     * Evaluates Java-like expressions in the context of a suspended thread.
     *
     * Supported: literals, variables, this, @N (an object shown earlier), `returned` and `exception`
     * at an exit stop, chained field access, array indexing, arithmetic (+,-,*,/,%), string concat,
     * comparisons, boolean operators (&&, ||, !), unary (-, !), grouping, and calls of the JDK methods
     * of METHODS_ALLOWED (any other call is refused).
     */
    private static class ExprEvaluator {

        static final String PROJECT_CALLS = "this call could run code of the program and is not allowed (allowed: "
                + "size, isEmpty on JDK collections and maps; get(int) on JDK lists; get, containsKey, contains with "
                + "a string, boxed or enum key on JDK hash maps and sets; length, charAt on strings; toString, equals "
                + "on strings and boxed values; getClass().getName()); read fields instead";

        /** The JDK methods the evaluator may call; none of them can run a method of the program. size and
         *  isEmpty only on the JDK collections JdkContainers knows (a wrapper may delegate to a collection of
         *  the program); get(int) on lists; get and containsKey on maps and contains on collections by a
         *  string, boxed or enum key, except sorted ones with a comparator (it decides equality there).
         *  JdkCalls answers them from fields. */
        static boolean allowedCall(ObjectReference obj, String name, List<Value> args) {
            int argc = args.size();
            if (obj instanceof StringReference) {
                return (name.equals("length") || name.equals("isEmpty") || name.equals("toString")) && argc == 0
                        || name.equals("charAt") && argc == 1
                        || name.equals("equals") && argc == 1;  // String.equals compares strings only
            }
            if (boxedValue(obj) != null) {
                return name.equals("toString") && argc == 0 || name.equals("equals") && argc == 1;
            }
            if (name.equals("getClass") && argc == 0) return true;
            if (obj instanceof ClassObjectReference) {
                return (name.equals("getName") || name.equals("getSimpleName")) && argc == 0;
            }
            Boolean map = JdkContainers.kind(obj);
            if (map == null) return false;
            if ((name.equals("size") || name.equals("isEmpty")) && argc == 0) return true;
            if (name.equals("get") && argc == 1 && args.get(0) instanceof IntegerValue) {
                return !map && JdkContainers.isList(obj);
            }
            if ((map ? name.equals("get") || name.equals("containsKey") : name.equals("contains")) && argc == 1) {
                return JdkContainers.byEquality(obj) && jdkKey(args.get(0));
            }
            return false;
        }

        /** A key whose hashCode and equals are the JDK's own: a string, a boxed value or an enum constant. */
        static boolean jdkKey(Value v) {
            if (v == null || v instanceof PrimitiveValue || v instanceof StringReference) return true;
            if (!(v instanceof ObjectReference)) return false;
            ObjectReference o = (ObjectReference) v;
            return boxedValue(o) != null || o.referenceType() instanceof ClassType && ((ClassType) o.referenceType()).isEnum();
        }

        // --- Token types ---
        enum TT {
            INTEGER, LONG, FLOAT, DOUBLE, STRING, CHAR,
            TRUE, FALSE, NULL, THIS, INSTANCEOF, REF,
            IDENT,
            PLUS, MINUS, STAR, SLASH, PERCENT,
            EQ, NEQ, LT, GT, LEQ, GEQ,
            AND, OR, NOT,
            DOT, LPAREN, RPAREN, LBRACKET, RBRACKET, COMMA,
            EOF
        }

        static class Token {
            final TT type;
            final String text;
            final int pos;
            Token(TT type, String text, int pos) {
                this.type = type; this.text = text; this.pos = pos;
            }
        }

        private final String source;
        private final List<Token> tokens;
        private int current;
        private final VirtualMachine vm;
        private final ThreadReference thread;
        private final StackFrame frame;
        private final Map<Long, ObjectReference> refs;
        private final Map<String, Value> extras;

        ExprEvaluator(String expression, VirtualMachine vm, ThreadReference thread, StackFrame frame,
                      Map<Long, ObjectReference> refs, Map<String, Value> extras) {
            this.source = expression == null ? "" : expression;
            this.vm = vm;
            this.thread = thread;
            this.frame = frame;
            this.refs = refs;
            this.extras = extras;
            this.tokens = tokenize(this.source);
            this.current = 0;
        }

        /** Evaluate the expression and return a JDI Value (null for Java null). */
        Value evaluate() {
            Value result = parseExpression();
            if (peek().type != TT.EOF) {
                throw error("Unexpected token after expression: " + peek().text);
            }
            return result;
        }

        // ---- Tokenizer ----

        private List<Token> tokenize(String src) {
            List<Token> toks = new ArrayList<>();
            int i = 0;
            int len = src.length();

            while (i < len) {
                char c = src.charAt(i);

                // Skip whitespace
                if (Character.isWhitespace(c)) { i++; continue; }

                // Numbers
                if (Character.isDigit(c) || (c == '.' && i + 1 < len && Character.isDigit(src.charAt(i + 1)))) {
                    int start = i;
                    boolean isFloat = false;
                    // Hex or binary prefix
                    if (c == '0' && i + 1 < len && (src.charAt(i + 1) == 'x' || src.charAt(i + 1) == 'X')) {
                        i += 2;
                        while (i < len && isHexDigit(src.charAt(i))) i++;
                    } else if (c == '0' && i + 1 < len && (src.charAt(i + 1) == 'b' || src.charAt(i + 1) == 'B')) {
                        i += 2;
                        while (i < len && (src.charAt(i) == '0' || src.charAt(i) == '1')) i++;
                    } else {
                        while (i < len && Character.isDigit(src.charAt(i))) i++;
                        if (i < len && src.charAt(i) == '.') {
                            isFloat = true; i++;
                            while (i < len && Character.isDigit(src.charAt(i))) i++;
                        }
                        // Scientific notation
                        if (i < len && (src.charAt(i) == 'e' || src.charAt(i) == 'E')) {
                            isFloat = true; i++;
                            if (i < len && (src.charAt(i) == '+' || src.charAt(i) == '-')) i++;
                            while (i < len && Character.isDigit(src.charAt(i))) i++;
                        }
                    }
                    // Suffix
                    if (i < len && (src.charAt(i) == 'L' || src.charAt(i) == 'l')) {
                        i++;
                        toks.add(new Token(TT.LONG, src.substring(start, i), start));
                    } else if (i < len && (src.charAt(i) == 'f' || src.charAt(i) == 'F')) {
                        i++;
                        toks.add(new Token(TT.FLOAT, src.substring(start, i), start));
                    } else if (i < len && (src.charAt(i) == 'd' || src.charAt(i) == 'D')) {
                        i++;
                        toks.add(new Token(TT.DOUBLE, src.substring(start, i), start));
                    } else if (isFloat) {
                        toks.add(new Token(TT.DOUBLE, src.substring(start, i), start));
                    } else {
                        toks.add(new Token(TT.INTEGER, src.substring(start, i), start));
                    }
                    continue;
                }

                // Strings
                if (c == '"') {
                    int start = i; i++;
                    StringBuilder sb = new StringBuilder();
                    while (i < len && src.charAt(i) != '"') {
                        if (src.charAt(i) == '\\' && i + 1 < len) {
                            i++;
                            switch (src.charAt(i)) {
                                case 'n': sb.append('\n'); break;
                                case 't': sb.append('\t'); break;
                                case 'r': sb.append('\r'); break;
                                case '\\': sb.append('\\'); break;
                                case '"': sb.append('"'); break;
                                case '\'': sb.append('\''); break;
                                default: sb.append(src.charAt(i));
                            }
                        } else {
                            sb.append(src.charAt(i));
                        }
                        i++;
                    }
                    if (i < len) i++; // consume closing "
                    toks.add(new Token(TT.STRING, sb.toString(), start));
                    continue;
                }

                // Chars
                if (c == '\'') {
                    int start = i; i++;
                    char ch;
                    if (i < len && src.charAt(i) == '\\' && i + 1 < len) {
                        i++;
                        switch (src.charAt(i)) {
                            case 'n': ch = '\n'; break;
                            case 't': ch = '\t'; break;
                            case 'r': ch = '\r'; break;
                            case '\\': ch = '\\'; break;
                            case '\'': ch = '\''; break;
                            default: ch = src.charAt(i);
                        }
                    } else if (i < len) {
                        ch = src.charAt(i);
                    } else {
                        throw new RuntimeException("Unterminated char literal at " + start);
                    }
                    i++;
                    if (i < len && src.charAt(i) == '\'') i++; // consume closing '
                    toks.add(new Token(TT.CHAR, String.valueOf(ch), start));
                    continue;
                }

                // @N: an object shown in an earlier answer
                if (c == '@' && i + 1 < len && Character.isDigit(src.charAt(i + 1))) {
                    int start = i; i++;
                    while (i < len && Character.isDigit(src.charAt(i))) i++;
                    toks.add(new Token(TT.REF, src.substring(start + 1, i), start));
                    continue;
                }

                // Identifiers and keywords
                if (Character.isJavaIdentifierStart(c)) {
                    int start = i;
                    while (i < len && Character.isJavaIdentifierPart(src.charAt(i))) i++;
                    String word = src.substring(start, i);
                    TT type;
                    switch (word) {
                        case "true": type = TT.TRUE; break;
                        case "false": type = TT.FALSE; break;
                        case "null": type = TT.NULL; break;
                        case "this": type = TT.THIS; break;
                        case "instanceof": type = TT.INSTANCEOF; break;
                        default: type = TT.IDENT;
                    }
                    toks.add(new Token(type, word, start));
                    continue;
                }

                // Two-char operators
                if (i + 1 < len) {
                    String two = src.substring(i, i + 2);
                    TT tt = null;
                    switch (two) {
                        case "==": tt = TT.EQ; break;
                        case "!=": tt = TT.NEQ; break;
                        case "<=": tt = TT.LEQ; break;
                        case ">=": tt = TT.GEQ; break;
                        case "&&": tt = TT.AND; break;
                        case "||": tt = TT.OR; break;
                    }
                    if (tt != null) {
                        toks.add(new Token(tt, two, i));
                        i += 2;
                        continue;
                    }
                }

                // Single-char operators
                TT tt = null;
                switch (c) {
                    case '+': tt = TT.PLUS; break;
                    case '-': tt = TT.MINUS; break;
                    case '*': tt = TT.STAR; break;
                    case '/': tt = TT.SLASH; break;
                    case '%': tt = TT.PERCENT; break;
                    case '<': tt = TT.LT; break;
                    case '>': tt = TT.GT; break;
                    case '!': tt = TT.NOT; break;
                    case '.': tt = TT.DOT; break;
                    case '(': tt = TT.LPAREN; break;
                    case ')': tt = TT.RPAREN; break;
                    case '[': tt = TT.LBRACKET; break;
                    case ']': tt = TT.RBRACKET; break;
                    case ',': tt = TT.COMMA; break;
                }
                if (tt != null) {
                    toks.add(new Token(tt, String.valueOf(c), i));
                    i++;
                    continue;
                }

                throw new RuntimeException("Unexpected character '" + c + "' at position " + i);
            }

            toks.add(new Token(TT.EOF, "", len));
            return toks;
        }

        private boolean isHexDigit(char c) {
            return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F');
        }

        // ---- Token helpers ----

        private Token peek() { return tokens.get(current); }
        private Token advance() { return tokens.get(current++); }
        private boolean check(TT type) { return peek().type == type; }

        private boolean match(TT... types) {
            for (TT t : types) {
                if (check(t)) { advance(); return true; }
            }
            return false;
        }

        private Token expect(TT type, String msg) {
            if (check(type)) return advance();
            throw error(msg + ", got '" + peek().text + "'");
        }

        private RuntimeException error(String msg) {
            return new RuntimeException(msg);
        }

        // ---- Parser (one method per precedence level) ----

        private Value parseExpression() { return parseOr(); }

        // Known limitation: this evaluator fuses parsing and evaluation, so the
        // right-hand operand of || and && is always evaluated to consume its
        // tokens even when Java's short-circuit semantics would skip it. Any
        // side effects (e.g. method calls) in a short-circuited RHS still occur,
        // unlike real Java. The final boolean result returned is still correct.
        private Value parseOr() {
            Value left = parseAnd();
            while (match(TT.OR)) {
                boolean l = toBoolean(left);
                Value right = parseAnd(); // always parse to consume tokens
                if (l) {
                    left = vm.mirrorOf(true); // short-circuit: left is truthy
                } else {
                    left = vm.mirrorOf(toBoolean(right));
                }
            }
            return left;
        }

        // Known limitation: see parseOr() above -- the RHS of && is always
        // evaluated (for side effects too) even when short-circuited.
        private Value parseAnd() {
            Value left = parseEquality();
            while (match(TT.AND)) {
                boolean l = toBoolean(left);
                Value right = parseEquality(); // always parse to consume tokens
                if (!l) {
                    left = vm.mirrorOf(false); // short-circuit: left is falsy
                } else {
                    left = vm.mirrorOf(toBoolean(right));
                }
            }
            return left;
        }

        private Value parseEquality() {
            Value left = parseComparison();
            while (check(TT.EQ) || check(TT.NEQ)) {
                TT op = advance().type;
                Value right = parseComparison();
                left = vm.mirrorOf(performEquality(left, op, right));
            }
            return left;
        }

        private Value parseComparison() {
            Value left = parseAddition();
            while (check(TT.LT) || check(TT.GT) || check(TT.LEQ) || check(TT.GEQ) || check(TT.INSTANCEOF)) {
                if (check(TT.INSTANCEOF)) {
                    advance();
                    // Read type name (may be dotted: java.lang.String)
                    StringBuilder typeName = new StringBuilder();
                    typeName.append(expect(TT.IDENT, "Expected type name after instanceof").text);
                    while (check(TT.DOT)) {
                        advance();
                        typeName.append('.').append(expect(TT.IDENT, "Expected type name").text);
                    }
                    left = vm.mirrorOf(performInstanceof(left, typeName.toString()));
                } else {
                    TT op = advance().type;
                    Value right = parseAddition();
                    left = vm.mirrorOf(performComparison(left, op, right));
                }
            }
            return left;
        }

        private Value parseAddition() {
            Value left = parseMultiplication();
            while (check(TT.PLUS) || check(TT.MINUS)) {
                TT op = advance().type;
                Value right = parseMultiplication();
                left = performArithmetic(left, op, right);
            }
            return left;
        }

        private Value parseMultiplication() {
            Value left = parseUnary();
            while (check(TT.STAR) || check(TT.SLASH) || check(TT.PERCENT)) {
                TT op = advance().type;
                Value right = parseUnary();
                left = performArithmetic(left, op, right);
            }
            return left;
        }

        private Value parseUnary() {
            if (match(TT.NOT)) {
                Value v = parseUnary();
                return vm.mirrorOf(!toBoolean(v));
            }
            if (match(TT.MINUS)) {
                Value v = parseUnary();
                v = unbox(v);
                if (v instanceof IntegerValue) return vm.mirrorOf(-((IntegerValue) v).value());
                if (v instanceof LongValue) return vm.mirrorOf(-((LongValue) v).value());
                if (v instanceof FloatValue) return vm.mirrorOf(-((FloatValue) v).value());
                if (v instanceof DoubleValue) return vm.mirrorOf(-((DoubleValue) v).value());
                throw error("Cannot negate non-numeric value");
            }
            return parsePostfix();
        }

        private Value parsePostfix() {
            Value result = parsePrimary();

            while (true) {
                if (match(TT.DOT)) {
                    Token name = expect(TT.IDENT, "Expected field or method name after '.'");
                    if (check(TT.LPAREN)) {
                        advance(); // consume '('
                        List<Value> args = parseArgList();
                        expect(TT.RPAREN, "Expected ')' after method arguments");
                        result = invokeMethod(result, name.text, args);
                    } else {
                        result = accessField(result, name.text);
                    }
                } else if (match(TT.LBRACKET)) {
                    Value index = parseExpression();
                    expect(TT.RBRACKET, "Expected ']' after array index");
                    result = arrayAccess(result, index);
                } else {
                    break;
                }
            }
            return result;
        }

        private Value parsePrimary() {
            Token t = peek();
            switch (t.type) {
                case INTEGER: {
                    advance();
                    String text = t.text;
                    if (text.startsWith("0x") || text.startsWith("0X")) {
                        return vm.mirrorOf(Integer.parseUnsignedInt(text.substring(2), 16));
                    } else if (text.startsWith("0b") || text.startsWith("0B")) {
                        return vm.mirrorOf(Integer.parseUnsignedInt(text.substring(2), 2));
                    }
                    return vm.mirrorOf(Integer.parseInt(text));
                }
                case LONG: {
                    advance();
                    String text = t.text;
                    // Strip L/l suffix
                    text = text.substring(0, text.length() - 1);
                    if (text.startsWith("0x") || text.startsWith("0X")) {
                        return vm.mirrorOf(Long.parseUnsignedLong(text.substring(2), 16));
                    } else if (text.startsWith("0b") || text.startsWith("0B")) {
                        return vm.mirrorOf(Long.parseUnsignedLong(text.substring(2), 2));
                    }
                    return vm.mirrorOf(Long.parseLong(text));
                }
                case FLOAT: {
                    advance();
                    return vm.mirrorOf(Float.parseFloat(t.text));
                }
                case DOUBLE: {
                    advance();
                    return vm.mirrorOf(Double.parseDouble(t.text));
                }
                case STRING: {
                    advance();
                    return vm.mirrorOf(t.text);
                }
                case CHAR: {
                    advance();
                    return vm.mirrorOf(t.text.charAt(0));
                }
                case TRUE: { advance(); return vm.mirrorOf(true); }
                case FALSE: { advance(); return vm.mirrorOf(false); }
                case NULL: { advance(); return null; }
                case REF: {
                    advance();
                    ObjectReference o = refs.get(Long.parseLong(t.text));
                    if (o == null) throw error("unknown object @" + t.text + " (only objects shown in this JVM run)");
                    return o;
                }
                case THIS: {
                    advance();
                    ObjectReference thisObj = frame.thisObject();
                    if (thisObj == null) throw error(NO_THIS);
                    return thisObj;
                }
                case IDENT: {
                    advance();
                    String name = t.text;
                    if (check(TT.LPAREN)) {
                        // a bare call is a call of a project method (this.m() or a static method)
                        throw error(PROJECT_CALLS);
                    }
                    return resolveVariable(name);
                }
                case LPAREN: {
                    advance();
                    Value val = parseExpression();
                    expect(TT.RPAREN, "Expected ')'");
                    return val;
                }
                default:
                    throw error("Unexpected token: " + t.text);
            }
        }

        private List<Value> parseArgList() {
            List<Value> args = new ArrayList<>();
            if (!check(TT.RPAREN)) {
                args.add(parseExpression());
                while (match(TT.COMMA)) {
                    args.add(parseExpression());
                }
            }
            return args;
        }

        // ---- JDI helpers ----

        private Value resolveVariable(String name) {
            // 1. Local variable
            try {
                LocalVariable lv = frame.visibleVariableByName(name);
                if (lv != null) return frame.getValue(lv);
            } catch (AbsentInformationException e) { /* fall through */ }
            if (extras.containsKey(name)) return extras.get(name);

            // 2. 'this' field
            ObjectReference thisObj = frame.thisObject();
            if (thisObj != null) {
                Field f = thisObj.referenceType().fieldByName(name);
                if (f != null) return thisObj.getValue(f);
            }

            // 3. Static field of enclosing class
            ReferenceType enclosing = frame.location().declaringType();
            Field sf = enclosing.fieldByName(name);
            if (sf != null && sf.isStatic()) return enclosing.getValue(sf);

            throw error("Cannot resolve variable: " + name);
        }

        private Value accessField(Value target, String fieldName) {
            if (target == null) throw error("Cannot access field '" + fieldName + "' on null");

            // array.length
            if (target instanceof ArrayReference && "length".equals(fieldName)) {
                return vm.mirrorOf(((ArrayReference) target).length());
            }

            if (!(target instanceof ObjectReference)) {
                throw error("Cannot access field '" + fieldName + "' on primitive value");
            }

            ObjectReference obj = (ObjectReference) target;
            if (restricted(obj.referenceType())) throw error(String.format(RESTRICTED, obj.referenceType().name()));

            // String.length() is common but 'length' is not a field — handle as special case
            Field field = obj.referenceType().fieldByName(fieldName);
            if (field == null) {
                throw error("No field '" + fieldName + "' on type " + obj.referenceType().name());
            }
            return obj.getValue(field);
        }

        private Value invokeMethod(Value target, String methodName, List<Value> args) {
            if (target == null) throw error("Cannot invoke '" + methodName + "()' on null");
            if (!(target instanceof ObjectReference)) {
                throw error("Cannot invoke method on primitive value");
            }

            ObjectReference obj = (ObjectReference) target;
            if (!allowedCall(obj, methodName, args)) throw error(PROJECT_CALLS);
            // every allowed call is answered from the fields of the object: no method runs in the JVM, so
            // nothing of the program (hashCode, equals, compareTo, toString) runs either
            try {
                return JdkCalls.answer(vm, obj, methodName, args);
            } catch (IllegalArgumentException e) {
                throw error(e.getMessage());
            }
        }

        /** Best-effort overload resolution: pick the first method whose arg types are compatible. */
        private Method bestMatch(List<Method> methods, List<Value> args) {
            for (Method m : methods) {
                try {
                    List<String> paramTypes = m.argumentTypeNames();
                    boolean compatible = true;
                    for (int i = 0; i < args.size(); i++) {
                        if (!isAssignable(args.get(i), paramTypes.get(i))) {
                            compatible = false;
                            break;
                        }
                    }
                    if (compatible) return m;
                } catch (Exception e) { /* skip */ }
            }
            return methods.get(0); // fallback: just try the first
        }

        private boolean isAssignable(Value arg, String paramTypeName) {
            if (arg == null) {
                // null is assignable to any reference type
                return !isPrimitiveTypeName(paramTypeName);
            }
            String argType = arg.type().name();
            if (argType.equals(paramTypeName)) return true;

            // Primitive widening
            if (arg instanceof PrimitiveValue) {
                return isWidenable(argType, paramTypeName);
            }

            // Auto-boxing
            String boxed = boxedName(paramTypeName);
            if (boxed != null && argType.equals(boxed)) return true;
            String unboxed = unboxedName(paramTypeName);
            if (unboxed != null && argType.equals(unboxed)) return true;

            // Subtype check
            if (arg instanceof ObjectReference) {
                ReferenceType argRefType = ((ObjectReference) arg).referenceType();
                try {
                    // Check if arg's type can be assigned to param type
                    List<ReferenceType> paramTypes = vm.classesByName(paramTypeName);
                    if (!paramTypes.isEmpty()) {
                        ReferenceType paramRefType = paramTypes.get(0);
                        return isSubtypeOf(argRefType, paramRefType);
                    }
                } catch (Exception e) { /* fall through */ }
            }

            return false;
        }

        private boolean isSubtypeOf(ReferenceType sub, ReferenceType sup) {
            if (sub.equals(sup)) return true;
            if (sub instanceof ClassType) {
                ClassType ct = (ClassType) sub;
                // Check interfaces (recurse to cover interface-extends-interface)
                for (InterfaceType iface : ct.interfaces()) {
                    if (isSubtypeOf(iface, sup)) return true;
                }
                // Check superclass
                ClassType superClass = ct.superclass();
                if (superClass != null) return isSubtypeOf(superClass, sup);
            } else if (sub instanceof InterfaceType) {
                InterfaceType it = (InterfaceType) sub;
                for (InterfaceType superIface : it.superinterfaces()) {
                    if (isSubtypeOf(superIface, sup)) return true;
                }
            }
            return false;
        }

        private Value arrayAccess(Value target, Value index) {
            if (target == null) throw error("Cannot index null");
            if (!(target instanceof ArrayReference)) {
                throw error("Cannot index non-array type: " + target.type().name());
            }
            index = unbox(index);
            if (!(index instanceof IntegerValue || index instanceof LongValue
                  || index instanceof ShortValue || index instanceof ByteValue)) {
                throw error("Array index must be an integer");
            }
            int i = (int) toLong(index);
            ArrayReference arr = (ArrayReference) target;
            if (i < 0 || i >= arr.length()) {
                throw error("Array index out of bounds: " + i + " (length " + arr.length() + ")");
            }
            return arr.getValue(i);
        }

        // ---- Arithmetic ----

        private Value performArithmetic(Value left, TT op, Value right) {
            // Unbox if needed
            Value ul = unbox(left), ur = unbox(right);

            // String concatenation
            if (op == TT.PLUS && (isStringLike(left) || isStringLike(right))) {
                return vm.mirrorOf(valueToString(left) + valueToString(right));
            }

            if (!isNumeric(ul) || !isNumeric(ur)) {
                throw error("Arithmetic requires numeric operands");
            }

            if (isFloatingPoint(ul) || isFloatingPoint(ur)) {
                double l = toDouble(ul), r = toDouble(ur);
                double res;
                switch (op) {
                    case PLUS: res = l + r; break;
                    case MINUS: res = l - r; break;
                    case STAR: res = l * r; break;
                    case SLASH: res = l / r; break;
                    case PERCENT: res = l % r; break;
                    default: throw error("Unknown arithmetic operator");
                }
                return vm.mirrorOf(res);
            } else {
                long l = toLong(ul), r = toLong(ur);
                long res;
                switch (op) {
                    case PLUS: res = l + r; break;
                    case MINUS: res = l - r; break;
                    case STAR: res = l * r; break;
                    case SLASH:
                        if (r == 0) throw error("Division by zero");
                        res = l / r; break;
                    case PERCENT:
                        if (r == 0) throw error("Division by zero");
                        res = l % r; break;
                    default: throw error("Unknown arithmetic operator");
                }
                // Preserve int type if both operands were int-sized
                if (ul instanceof IntegerValue && ur instanceof IntegerValue) {
                    return vm.mirrorOf((int) res);
                }
                return vm.mirrorOf(res);
            }
        }

        private boolean performEquality(Value left, TT op, Value right) {
            boolean eq;
            if (left == null && right == null) {
                eq = true;
            } else if (left == null || right == null) {
                eq = false;
            } else {
                Value ul = unbox(left), ur = unbox(right);
                if (isNumeric(ul) && isNumeric(ur)) {
                    if (isFloatingPoint(ul) || isFloatingPoint(ur)) {
                        eq = toDouble(ul) == toDouble(ur);
                    } else {
                        eq = toLong(ul) == toLong(ur);
                    }
                } else if (ul instanceof BooleanValue && ur instanceof BooleanValue) {
                    eq = ((BooleanValue) ul).value() == ((BooleanValue) ur).value();
                } else if (ul instanceof CharValue && ur instanceof CharValue) {
                    eq = ((CharValue) ul).value() == ((CharValue) ur).value();
                } else if (left instanceof ObjectReference && right instanceof ObjectReference) {
                    // Reference equality (Java == semantics)
                    eq = ((ObjectReference) left).uniqueID() == ((ObjectReference) right).uniqueID();
                } else {
                    eq = false;
                }
            }
            return op == TT.EQ ? eq : !eq;
        }

        private boolean performComparison(Value left, TT op, Value right) {
            Value ul = unbox(left), ur = unbox(right);
            if (!isNumeric(ul) || !isNumeric(ur)) {
                throw error("Comparison requires numeric operands");
            }
            int cmp;
            if (isFloatingPoint(ul) || isFloatingPoint(ur)) {
                cmp = Double.compare(toDouble(ul), toDouble(ur));
            } else {
                cmp = Long.compare(toLong(ul), toLong(ur));
            }
            switch (op) {
                case LT: return cmp < 0;
                case GT: return cmp > 0;
                case LEQ: return cmp <= 0;
                case GEQ: return cmp >= 0;
                default: throw error("Unknown comparison operator");
            }
        }

        private boolean performInstanceof(Value left, String typeName) {
            if (left == null) return false;
            if (!(left instanceof ObjectReference)) return false;
            ReferenceType objType = ((ObjectReference) left).referenceType();
            // Check by name match or subtype
            if (objType.name().equals(typeName)) return true;
            List<ReferenceType> targetTypes = vm.classesByName(typeName);
            if (targetTypes.isEmpty()) {
                // Try simple name match against loaded classes
                for (ReferenceType rt : vm.allClasses()) {
                    if (rt.name().endsWith("." + typeName) || rt.name().equals(typeName)) {
                        targetTypes = Collections.singletonList(rt);
                        break;
                    }
                }
            }
            if (targetTypes.isEmpty()) return false;
            return isSubtypeOf(objType, targetTypes.get(0));
        }

        // ---- Unboxing ----

        private Value unbox(Value v) {
            if (v == null || v instanceof PrimitiveValue) return v;
            if (!(v instanceof ObjectReference)) return v;
            Value inner = boxedValue((ObjectReference) v);
            return inner != null ? inner : v;
        }

        // ---- Type helpers ----

        private boolean isNumeric(Value v) {
            return v instanceof IntegerValue || v instanceof LongValue
                || v instanceof FloatValue || v instanceof DoubleValue
                || v instanceof ShortValue || v instanceof ByteValue;
        }

        private boolean isFloatingPoint(Value v) {
            return v instanceof FloatValue || v instanceof DoubleValue;
        }

        private boolean isStringLike(Value v) {
            return v instanceof StringReference;
        }

        private boolean toBoolean(Value v) {
            if (v == null) return false;
            v = unbox(v);
            if (v instanceof BooleanValue) return ((BooleanValue) v).value();
            if (v instanceof IntegerValue) return ((IntegerValue) v).value() != 0;
            if (v instanceof LongValue) return ((LongValue) v).value() != 0;
            return true; // objects are truthy
        }

        private long toLong(Value v) {
            if (v instanceof IntegerValue) return ((IntegerValue) v).value();
            if (v instanceof LongValue) return ((LongValue) v).value();
            if (v instanceof ShortValue) return ((ShortValue) v).value();
            if (v instanceof ByteValue) return ((ByteValue) v).value();
            if (v instanceof CharValue) return ((CharValue) v).value();
            throw error("Cannot convert to long: " + (v != null ? v.type().name() : "null"));
        }

        private double toDouble(Value v) {
            if (v instanceof DoubleValue) return ((DoubleValue) v).value();
            if (v instanceof FloatValue) return ((FloatValue) v).value();
            return (double) toLong(v);
        }

        private String valueToString(Value v) {
            if (v == null) return "null";
            if (v instanceof StringReference) return ((StringReference) v).value();
            if (v instanceof PrimitiveValue) {
                return v instanceof CharValue ? String.valueOf(((CharValue) v).value()) : v.toString();
            }
            if (v instanceof ObjectReference) {
                Value inner = boxedValue((ObjectReference) v);
                if (inner != null) return valueToString(inner);
                ObjectReference obj = (ObjectReference) v;
                return obj.referenceType().name() + "@" + obj.uniqueID();  // no toString of project objects
            }
            return String.valueOf(v);
        }

        private boolean isPrimitiveTypeName(String name) {
            switch (name) {
                case "int": case "long": case "float": case "double":
                case "boolean": case "char": case "short": case "byte":
                    return true;
                default: return false;
            }
        }

        private boolean isWidenable(String from, String to) {
            // Java primitive widening conversions
            switch (from) {
                case "byte": return "short".equals(to) || "int".equals(to) || "long".equals(to) || "float".equals(to) || "double".equals(to);
                case "short": return "int".equals(to) || "long".equals(to) || "float".equals(to) || "double".equals(to);
                case "char": return "int".equals(to) || "long".equals(to) || "float".equals(to) || "double".equals(to);
                case "int": return "long".equals(to) || "float".equals(to) || "double".equals(to);
                case "long": return "float".equals(to) || "double".equals(to);
                case "float": return "double".equals(to);
                default: return false;
            }
        }

        private String boxedName(String primitive) {
            switch (primitive) {
                case "int": return "java.lang.Integer";
                case "long": return "java.lang.Long";
                case "float": return "java.lang.Float";
                case "double": return "java.lang.Double";
                case "boolean": return "java.lang.Boolean";
                case "char": return "java.lang.Character";
                case "short": return "java.lang.Short";
                case "byte": return "java.lang.Byte";
                default: return null;
            }
        }

        private String unboxedName(String boxed) {
            switch (boxed) {
                case "java.lang.Integer": return "int";
                case "java.lang.Long": return "long";
                case "java.lang.Float": return "float";
                case "java.lang.Double": return "double";
                case "java.lang.Boolean": return "boolean";
                case "java.lang.Character": return "char";
                case "java.lang.Short": return "short";
                case "java.lang.Byte": return "byte";
                default: return null;
            }
        }
    }

    // Minimal recursive-descent JSON parser
    private static class JsonParser {
        private final String input;
        private int pos;

        JsonParser(String input) {
            this.input = input;
            this.pos = 0;
        }

        Object parseValue() {
            skipWhitespace();
            if (pos >= input.length()) return null;
            char c = input.charAt(pos);
            if (c == '{') return parseObject();
            if (c == '[') return parseArray();
            if (c == '"') return parseString();
            if (c == 't' || c == 'f') return parseBoolean();
            if (c == 'n') return parseNull();
            return parseNumber();
        }

        Map<String, Object> parseObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            pos++; // skip '{'
            skipWhitespace();
            if (pos < input.length() && input.charAt(pos) == '}') { pos++; return map; }
            while (pos < input.length()) {
                skipWhitespace();
                String key = parseString();
                skipWhitespace();
                pos++; // skip ':'
                Object value = parseValue();
                map.put(key, value);
                skipWhitespace();
                if (pos < input.length() && input.charAt(pos) == ',') { pos++; continue; }
                break;
            }
            if (pos < input.length() && input.charAt(pos) == '}') pos++;
            return map;
        }

        List<Object> parseArray() {
            List<Object> list = new ArrayList<>();
            pos++; // skip '['
            skipWhitespace();
            if (pos < input.length() && input.charAt(pos) == ']') { pos++; return list; }
            while (pos < input.length()) {
                list.add(parseValue());
                skipWhitespace();
                if (pos < input.length() && input.charAt(pos) == ',') { pos++; continue; }
                break;
            }
            if (pos < input.length() && input.charAt(pos) == ']') pos++;
            return list;
        }

        String parseString() {
            pos++; // skip opening '"'
            StringBuilder sb = new StringBuilder();
            while (pos < input.length()) {
                char c = input.charAt(pos);
                if (c == '"') { pos++; return sb.toString(); }
                if (c == '\\') {
                    pos++;
                    if (pos >= input.length()) break;
                    char esc = input.charAt(pos);
                    switch (esc) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case '/': sb.append('/'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'b': sb.append('\b'); break;
                        case 'f': sb.append('\f'); break;
                        case 'u':
                            if (pos + 5 > input.length()) throw new RuntimeException("Unterminated \\u escape in JSON string");
                            String hex = input.substring(pos + 1, pos + 5);
                            sb.append((char) Integer.parseInt(hex, 16));
                            pos += 4;
                            break;
                        default: sb.append(esc);
                    }
                } else {
                    sb.append(c);
                }
                pos++;
            }
            return sb.toString();
        }

        Object parseNumber() {
            int start = pos;
            if (pos < input.length() && input.charAt(pos) == '-') pos++;
            while (pos < input.length() && Character.isDigit(input.charAt(pos))) pos++;
            boolean isFloat = false;
            if (pos < input.length() && input.charAt(pos) == '.') {
                isFloat = true;
                pos++;
                while (pos < input.length() && Character.isDigit(input.charAt(pos))) pos++;
            }
            if (pos < input.length() && (input.charAt(pos) == 'e' || input.charAt(pos) == 'E')) {
                isFloat = true;
                pos++;
                if (pos < input.length() && (input.charAt(pos) == '+' || input.charAt(pos) == '-')) pos++;
                while (pos < input.length() && Character.isDigit(input.charAt(pos))) pos++;
            }
            String numStr = input.substring(start, pos);
            if (isFloat) return Double.parseDouble(numStr);
            long l = Long.parseLong(numStr);
            if (l >= Integer.MIN_VALUE && l <= Integer.MAX_VALUE) return (int) l;
            return l;
        }

        Boolean parseBoolean() {
            if (input.startsWith("true", pos)) { pos += 4; return true; }
            if (input.startsWith("false", pos)) { pos += 5; return false; }
            throw new RuntimeException("Invalid boolean at " + pos);
        }

        Object parseNull() {
            if (input.startsWith("null", pos)) { pos += 4; return null; }
            throw new RuntimeException("Invalid null at " + pos);
        }

        void skipWhitespace() {
            while (pos < input.length() && Character.isWhitespace(input.charAt(pos))) pos++;
        }
    }

    // ========== Map/JSON Helpers ==========

    private static String str(Map<String, Object> m, String key) {
        if (m == null) return null;
        Object v = m.get(key);
        return v != null ? String.valueOf(v) : null;
    }

    private static String strOr(Map<String, Object> m, String key, String def) {
        String v = str(m, key);
        return v != null ? v : def;
    }

    private static int intVal(Map<String, Object> m, String key) {
        if (m == null) return 0;
        Object v = m.get(key);
        if (v instanceof Number) return ((Number) v).intValue();
        if (v instanceof String) try { return Integer.parseInt((String) v); } catch (NumberFormatException e) { /* */ }
        return 0;
    }

    private static long longVal(Map<String, Object> m, String key) {
        if (m == null) return 0L;
        Object v = m.get(key);
        if (v instanceof Number) return ((Number) v).longValue();
        if (v instanceof String) try { return Long.parseLong((String) v); } catch (NumberFormatException e) { /* */ }
        return 0L;
    }

    private static Integer intValOrNull(Map<String, Object> m, String key) {
        if (m == null) return null;
        Object v = m.get(key);
        if (v instanceof Number) return ((Number) v).intValue();
        if (v instanceof String) try { return Integer.parseInt((String) v); } catch (NumberFormatException e) { /* */ }
        return null;
    }

    private static boolean boolVal(Map<String, Object> m, String key, boolean def) {
        if (m == null) return def;
        Object v = m.get(key);
        if (v instanceof Boolean) return (Boolean) v;
        if (v instanceof String) return Boolean.parseBoolean((String) v);
        return def;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> map(Map<String, Object> m, String key) {
        if (m == null) return null;
        Object v = m.get(key);
        return v instanceof Map ? (Map<String, Object>) v : null;
    }

    @SuppressWarnings("unchecked")
    private static List<Object> list(Map<String, Object> m, String key) {
        if (m == null) return null;
        Object v = m.get(key);
        return v instanceof List ? (List<Object>) v : null;
    }

    @SuppressWarnings("unchecked")
    private static Map<String, Object> asMap(Object obj) {
        return obj instanceof Map ? (Map<String, Object>) obj : new HashMap<>();
    }

    private static Map<String, Object> mapOf(String key, Object value) {
        Map<String, Object> m = new HashMap<>();
        m.put(key, value);
        return m;
    }

    private static void log(String msg) {
        System.err.println("[JdiDapServer] " + msg);
    }

    private static void logVerbose(String msg) {
        if (debug) System.err.println("[JdiDapServer] " + msg);
    }
}
