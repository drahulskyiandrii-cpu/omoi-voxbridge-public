"""Synthetic German/Russian captions; zero network, GPU or microphone use."""
from voxbridge.captions_core import CaptionCoordinator
from voxbridge.captions_translate import translate_committed


class MockTranslator:
    is_cloud = True
    translations = {
        "Ein VLAN trennt logische Netze.": "VLAN разделяет логические сети.",
        "DHCP verteilt automatisch IP-Adressen.": "DHCP автоматически выдаёт IP-адреса.",
    }

    def generate(self, system: str, user: str) -> str:
        del system
        segment = user.rsplit("CURRENT finalized segment to translate:\n", 1)[-1]
        return self.translations[segment]


def main():
    controller = CaptionCoordinator()
    generation = controller.begin(source_language="de", target_language="ru")
    translator = MockTranslator()
    assert controller.partial(generation, 0, 0, "Ein VLAN trennt ...")
    assert controller.take_translation() is None
    for sequence, source in enumerate(translator.translations):
        assert controller.final(generation, sequence, source)
        job = controller.take_translation()
        assert job is not None
        result = translate_committed(job, translator, cloud_approved=True)
        assert controller.finish_translation(job, result)
    snapshot = controller.snapshot()
    assert len(snapshot.lines) == 2
    assert all(line.status == "complete" for line in snapshot.lines)
    for line in snapshot.lines:
        print("seq=" + str(line.sequence) + ": " + line.status)
    controller.stop()
    print("MOCK_CAPTIONS_SMOKE_PASS: zero HTTP calls, zero local inference")


if __name__ == "__main__":
    main()
