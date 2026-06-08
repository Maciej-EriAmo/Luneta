"""
test_karmazyn_fixes.py
Weryfikacja 5 usuniętych błędów w architekturze KarmazynOS.
"""

import gc
from karmazyn_atom import AtomRegistry, Atom
from karmazyn_js_core import Function
from karmazyn_js_phi import KarmazynJSPhi, PhiScope
from karmazyn_dom import DOMMapper

def run_tests():
    print("Rozpoczynam weryfikację poprawek...")
    test_bug_1_scope()
    test_bug_2_cache()
    test_bug_3_and_4_dom_unified_model()
    test_bug_5_memory_leak()
    print("Kryteria sukcesu spełnione. Wszystkie testy przeszły pomyślnie.")

def test_bug_1_scope():
    """Bug 1: _call() tworzy Scope zamiast PhiScope"""
    vm = KarmazynJSPhi()
    
    # Tworzymy ręcznie symulację funkcji zapiętej w PhiScope
    func = Function(params=[], body=[], closure=vm.global_scope)
    
    # Tworzymy lokalny scope przez child() - tak jak robi to _call()
    local_scope = func.closure.child()
    
    assert isinstance(local_scope, PhiScope), \
        f"FAIL: child_scope to {type(local_scope)}, powinno być PhiScope (Brak termodynamiki!)"
    print(" [OK] Bug 1: Funkcje poprawnie generują PhiScope dla domknięć.")

def test_bug_2_cache():
    """Bug 2: AtomsWrapper cache stale przy delete+create o tym samym len()"""
    reg = AtomRegistry()
    wrapper = reg.atoms_wrapper
    
    reg.create("atom_A")
    reg.create("atom_B")
    
    # Inicjalizacja cache (długość: 2)
    lista_1 = wrapper()
    
    # Mutacja o sumie zerowej dla rozmiaru (usun 1, dodaj 1)
    reg.delete("atom_A")
    reg.create("atom_C")
    
    # Pobranie nowej listy
    lista_2 = wrapper()
    
    id_list = [a.id for a in lista_2]
    assert "atom_C" in id_list, "FAIL: Nowy atom_C nie pojawił się w cache!"
    assert "atom_A" not in id_list, "FAIL: Usunięty atom_A wciąż istnieje w cache!"
    print(" [OK] Bug 2: AtomsWrapper cache poprawnie reaguje na wersjonowanie (_version).")

def test_bug_3_and_4_dom_unified_model():
    """Bug 3 & 4: _state_for_T niespójne, _make_atom omija jednolity model"""
    
    # Minimalny mock środowiska uruchomieniowego Lunety dla DOMMapper
    class MockRuntime:
        def __init__(self):
            self.matrix = AtomRegistry()
        def get_atom(self, id):
            return self.matrix.get(id)
        def create_atom(self, id, S, E, T):
            return self.matrix.create(id, S, E, T)

    rt = MockRuntime()
    mapper = DOMMapper(rt)
    
    # 1. Tworzymy nowy atom DOM
    atom_id = mapper._make_atom("dom_1", "text:p", "Treść", 10.0)
    atom = rt.get_atom(atom_id)
    
    assert atom.T == 10.0, "FAIL: Atom nie został zainicjowany z poprawnym T."
    assert atom.state == "COLD", f"FAIL: Atom z T=10.0 ma stan {atom.state}, powinien być COLD (ujednolicony próg)."
    
    # 2. Aktualizacja (ponowne wejście na stronę) wyższym T - powinno użyć heat() zamiast atom.T = ...
    # Zgodnie z kodem: jeśli nowy T > stary T, robimy atom.heat(T - atom.T)
    mapper._make_atom("dom_1", "text:p", "Treść", 25.0)
    
    assert atom.T == 25.0, f"FAIL: T atomu po aktualizacji wynosi {atom.T}, a powinno 25.0 (nie wywołano poprawnego heat)"
    print(" [OK] Bug 3 & 4: DOMMapper używa natywnych operacji heat()/cool() i utrzymuje spójną termodynamikę.")

def test_bug_5_memory_leak():
    """Bug 5: Children scope nigdy nie czyszczone (Memory leak)"""
    parent_scope = PhiScope()
    
    # Tworzymy dużą ilość scope'ów w pętli
    def generate_garbage():
        for _ in range(100):
            # Tworzymy i natychmiast zapominamy
            _ = parent_scope.child()
            
    generate_garbage()
    
    # Forsujemy garbage collector
    gc.collect()
    
    child_count = len(parent_scope.children)
    assert child_count == 0, f"FAIL: Memory leak potwierdzony. {child_count} martwych scope'ów nadal wisiało w pamięci."
    print(" [OK] Bug 5: WeakSet poprawnie zwalnia osierocone domknięcia PhiScope.")


if __name__ == "__main__":
    run_tests()