package demo;

import static org.junit.jupiter.api.Assertions.assertEquals;

import org.junit.jupiter.api.Test;

class ShelfScenarioTest {
    @Test
    void scenario() {
        Shelf s = new Shelf();
        assertEquals(1, s.put("x"));
        assertEquals(2, s.put("y"));
        Tag.armed = true;  // from here on, every marker on standard error comes from the debugger
        assertEquals(3, s.fixedSize());
        System.err.println("SCENARIO_DONE");
    }
}
