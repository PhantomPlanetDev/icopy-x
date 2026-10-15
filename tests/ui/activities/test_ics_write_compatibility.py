import copy
import sys
import types

import pytest

import ics_credentials
import ics_decoder
from _constants import KEY_OK
from tests.ui.activities.test_ics_decoder import activity_stack, store, start, validated_capture


def pacs_capture(field='PACS', bits=26, frame=None, legacy=False):
    if frame is None:
        frame = ics_decoder.calculate_wiegand26_parity(165, 1551)
    payload = '06' + (frame << 6).to_bytes(4, 'big').hex()
    body = 'Bit#:{}\n{}#:{}'.format(bits, field, payload)
    if legacy:
        body += '\nBlk7#:1122334455667788'
    return ics_decoder.parse_block('$A_CARD_START$\n' + body + '\n$A_CARD_STOP$')


def mock_pm3(monkeypatch, hf_present=False):
    commands = []
    output = ['']
    executor = types.ModuleType('executor')

    def run(command, **kwargs):
        commands.append(command)
        if command.startswith('hf iclass rdbl'):
            output[0] = 'block 06: 00 00 80 1E C2 00 0A 7A' if hf_present else 'No tag found'
        elif command == 'lf t55xx detect':
            output[0] = 'Chip type: T55x7'
        elif command.startswith('lf hid clone'):
            output[0] = 'Done'
        elif command == 'lf hid reader':
            output[0] = 'H10301 FC: 165 CN: 1551'
        else:
            raise AssertionError('Unexpected command: ' + command)
        return 1

    executor.startPM3Task = run
    executor.getPrintContent = lambda: output[0]
    monkeypatch.setitem(sys.modules, 'executor', executor)
    return commands


@pytest.mark.parametrize('field', ['PACS', 'SIO', 'Hex'])
def test_pacs_h10301_without_wiegand_is_write_eligible(field):
    capture = pacs_capture(field)
    assert 'wiedata' not in capture
    assert 'reported_fc' not in capture
    assert capture['status'] == 'decoded'
    assert ics_credentials.validated_h10301(capture)
    assert ics_credentials.can_write(capture)


def test_direct_wiegand_and_independent_legacy_support():
    assert ics_credentials.validated_h10301(validated_capture())
    legacy = ics_decoder.parse_block(
        '$A_CARD_START$\nBit#:48\nBlk7#:1122334455667788\n$A_CARD_STOP$')
    assert ics_credentials.legacy_block(legacy) == '1122334455667788'
    assert ics_credentials.can_write(legacy)
    assert not ics_credentials.validated_h10301(legacy)


@pytest.mark.parametrize('bits', [0, 25, 34, 48, 56])
def test_other_formats_do_not_gain_pacs_write_eligibility(bits):
    assert not ics_credentials.can_write(pacs_capture(bits=bits))


@pytest.mark.parametrize('body', [
    'Bit#:26\nPACS#:not-hex',
    'Bit#:26\nPACS#:06',
    'Bit#:26\nPACS#:0800000000',
    'Bit#:26\nPACS#:85060600000000',
    'Bit#:26\nPACS#:06FFFFFFFF',
    'PACS#:0601488C07C0',
])
def test_malformed_or_unqualified_pacs_is_not_write_eligible(body):
    capture = ics_decoder.parse_block('$A_CARD_START$\n' + body + '\n$A_CARD_STOP$')
    assert not ics_credentials.can_write(capture)


def test_invalid_pacs_parity_is_not_write_eligible():
    frame = ics_decoder.calculate_wiegand26_parity(165, 1551) ^ 1
    assert not ics_credentials.can_write(pacs_capture(frame=frame))


def test_raw_sio_container_is_never_used_as_decrypted_pacs():
    capture = pacs_capture(field='SIO_CONTAINER')
    assert capture['status'] == 'unsupported'
    assert not ics_credentials.can_write(capture)
    forged = dict(capture, status='decoded', format='H10301 (26-bit)', fc=165, id=1551)
    assert not ics_credentials.can_write(forged)


@pytest.mark.parametrize('field,value', [
    ('fc', 12), ('id', 34), ('bit_length', 34), ('raw', '0x1'),
    ('sio_pacs', '0600000000'), ('raw_fields', {}),
    ('raw_response_text', 'changed'), ('raw_response_b64', 'invalid'),
    ('status', 'malformed'), ('status', 'decoder_error'),
])
def test_pacs_eligibility_revalidates_original_capture(field, value):
    capture = pacs_capture()
    capture[field] = value
    assert not ics_credentials.can_write(capture)


def test_hf_detection_still_precedes_lf(monkeypatch):
    commands = mock_pm3(monkeypatch)
    assert ics_decoder.detect_target() == 'lf_t5577'
    assert len(commands) == 5
    assert all(command.startswith('hf iclass rdbl') for command in commands[:4])
    assert commands[-1] == 'lf t55xx detect'


def test_positive_hf_detection_still_skips_lf(monkeypatch):
    commands = mock_pm3(monkeypatch, hf_present=True)
    assert ics_decoder.detect_target() == 'hf_iclass'
    assert len(commands) == 1
    assert commands[0].startswith('hf iclass rdbl')


@pytest.mark.parametrize('saved', [False, True])
def test_live_and_saved_pacs_require_confirmation_and_explicit_verify(store, monkeypatch, saved):
    commands = mock_pm3(monkeypatch)
    activity = start()
    capture = pacs_capture()
    original = copy.deepcopy(capture)
    monkeypatch.setattr(activity, '_start_target_poll', lambda: None)
    monkeypatch.setattr(activity, 'startBGTask', lambda job: job())
    if saved:
        store.save(capture, 'Synthetic PACS credential')
        activity._selected_record = store.all()[0]
        activity._show_saved_actions()
    else:
        activity._state = activity.STATE_READING
        serial_port = object()
        activity._ser = serial_port
        activity._on_read_finished(capture, serial_port, activity._read_generation)
        activity._ser = None
    assert commands == []
    activity._menu.setSelection(activity._menu_actions.index('write'))
    activity.onKeyEvent(KEY_OK)
    assert activity._state == activity.STATE_WAIT_BLANK
    activity._poll_target()
    assert activity._target_type == 'lf_t5577'
    assert activity._target_write_available()
    assert not any(command.startswith('lf hid clone') for command in commands)
    activity.onKeyEvent(KEY_OK)
    assert commands.count('lf t55xx detect') == 2
    assert commands[-1] == 'lf hid clone -w H10301 --fc 165 --cn 1551'
    assert activity._last_write_ok
    assert 'lf hid reader' not in commands
    activity._menu.setSelection(activity._menu_actions.index('verify'))
    activity.onKeyEvent(KEY_OK)
    assert commands[-1] == 'lf hid reader'
    assert activity._verify_success
    assert activity._source_data == original
    if saved:
        assert store.all()[0]['capture'] == original


@pytest.mark.parametrize('actual', [None, 'hf_iclass', 'unknown'])
def test_pacs_write_rechecks_target_and_never_dispatches_on_change(store, monkeypatch, actual):
    commands = mock_pm3(monkeypatch)
    activity = start()
    activity._source_data = pacs_capture(legacy=True)
    activity._target_type = 'lf_t5577'
    monkeypatch.setattr(ics_decoder, 'detect_target', lambda: actual)
    activity._do_write()
    assert commands == []
    assert activity._last_write_ok is False
    assert activity._write_blocked_reason == 'target_not_compatible'
