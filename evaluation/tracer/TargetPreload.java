/**
 * Java agent that loads (without initialising) the traced target class at JVM start-up.
 *
 * Why: BTrace instruments a class when it is loaded. If the first load happens while the test holds a
 * static mock of java.lang.Runtime (Mockito mockStatic, e.g. Checkstyle MainTest), BTrace's transformer
 * fails on the loading thread and the class stays uninstrumented, so the trace is empty. Loading the
 * class early, before any test code runs, avoids that. Classes that are not on the system class path
 * (e.g. in the Maven launcher JVM) are skipped silently. Loading without initialisation does not run
 * static initialisers, so the behaviour of the program is unchanged.
 */
public final class TargetPreload {
    private TargetPreload() {}

    public static void premain(String args) {
        if (args == null) return;
        for (String name : args.split(",")) {
            try {
                Class.forName(name.trim(), false, ClassLoader.getSystemClassLoader());
            } catch (Throwable ignored) {
            }
        }
    }
}
