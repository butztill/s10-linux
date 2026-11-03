#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
# Synthetic test identities only; this suite does not use a SIM or live network.
"""Exercise actual SIP parsing/registration/capabilities, without SIM/network."""
import collections
import json
import unittest
from unittest.mock import Mock, patch

import imsd
from ims_call import Calls
from sip_tcp import SipConnection, MAX_FRAME


class Tests(unittest.TestCase):
    def setUp(self):
        self.ims = imsd.Ims.__new__(imsd.Ims)
        for key, value in dict(src='2001:db8::2', imsi='001010000000001', imei='000000000000000',
                               domain='ims.example.invalid', impu='sip:test@example.invalid',
                               tag='local', call_id='register-private-id', cseq=0,
                               us=23001, uc=23000, pref_id=None, pani='IEEE-802.11;country=DE',
                               sec_verify='ipsec-3gpp;alg=hmac-sha-1-96', service_route=None,
                               registered=True).items():
            setattr(self.ims, key, value)
        self.ims.ss, self.ims.sc = Mock(), Mock()
        self.ims.ss.sendto.return_value = 200
        self.daemon = imsd.Daemon.__new__(imsd.Daemon)
        self.daemon.ims = self.ims
        self.daemon.sip_counts = collections.Counter()
        self.daemon.sip_seen = collections.OrderedDict()
        self.daemon.emit, self.daemon.on_rp = Mock(), Mock()
        self.logs = []
        self.logger = patch('imsd.log', lambda *args: self.logs.append(args))
        self.logger.start()
        self.addCleanup(self.logger.stop)
        self.media, self.stop = Mock(), Mock()
        self.daemon.calls = Calls(self.ims, self.daemon.emit, lambda *args: self.logs.append(args), self.media, self.stop)
        self.peer = ('2001:db8::1', 32001, 0, 0)

    def request(self, method='OPTIONS', extra=(), body=b''):
        return ('\r\n'.join(['%s sip:test@example.invalid SIP/2.0' % method,
                              'Via: SIP/2.0/UDP [2001:db8::1]:32001;branch=z9hG4bK-test',
                              'From: <sip:synthetic@example.invalid>;tag=remote',
                              'To: <sip:test@example.invalid>', 'Call-ID: private-test-id',
                              'CSeq: 1 ' + method] + list(extra) +
                             ['Content-Length: %d' % len(body)]) + '\r\n\r\n').encode() + body

    def sent(self):
        raw, peer = self.ims.ss.sendto.call_args.args
        status, hdr, body = imsd.parse(raw.decode('latin1'))
        return status, hdr, body

    def contact(self, port=23001, expiry=7200):
        return ('<sip:synthetic@[2001:db8::2]:%d;transport=udp>;expires=%d;audio;'
                '+g.3gpp.icsi-ref="urn%%3Aurn-7%%3A3gpp-service.ims.icsi.mmtel";'
                '+g.3gpp.smsip' % (port, expiry))

    def test_parse_compact_and_folded_headers_keeps_binary_body(self):
        data = 'MESSAGE sip:test SIP/2.0\r\nv: first\r\nv: second\r\nf: sender\r\nt: target\r\ni: id\r\nc: application/vnd.3gpp.sms\r\nk: sec-agree,\r\n  path\r\nm: <sip:test>;expires=1\r\nl: 2\r\n\r\n\x00\xff'
        status, headers, body = imsd.parse(data)
        self.assertEqual(headers['via'], ['first', 'second'])
        self.assertEqual(headers['supported'], ['sec-agree, path'])
        self.assertEqual(headers['content-type'], ['application/vnd.3gpp.sms'])
        self.assertEqual(headers['contact'], ['<sip:test>;expires=1'])
        self.assertEqual(body.encode('latin1'), b'\x00\xff')

    def test_combined_contacts_do_not_split_feature_tag_commas(self):
        first = self.contact() + ';methods="INVITE,MESSAGE"'
        second = self.contact(23003, 0)
        self.assertEqual(list(imsd.header_values([first + ', ' + second])), [first, second])

    def test_summary_selects_own_binding_not_tombstone(self):
        for contacts in ([self.contact(23003, 0), self.contact()],
                         [self.contact() + ', ' + self.contact(23003, 0)]):
            result = imsd.binding_summary({'contact': contacts}, self.ims.src, self.ims.us)
            self.assertEqual(result, dict(contacts=2, own_active=True, own_expires=7200,
                                         own_audio=True, own_mmtel=True, own_smsip=True))
            self.assertNotIn('synthetic', json.dumps(result))

    def test_deleted_missing_or_other_binding_is_not_active(self):
        for contacts in ([], [self.contact(expiry=0)], [self.contact(23003)],
                         [self.contact().replace(';expires=7200', '')]):
            result = imsd.binding_summary({'contact': contacts}, self.ims.src, self.ims.us)
            self.assertFalse(result['own_active'])

    def test_register_advertises_implemented_call_methods(self):
        raw = self.ims.register_msg(23001, 'synthetic-auth', 'ipsec-3gpp')
        _, headers, _ = imsd.parse(raw)
        self.assertIn('INVITE', headers['allow'][0])
        self.assertIn('ACK', headers['allow'][0])
        self.assertNotIn('PUBLISH', headers['allow'][0])
        self.assertNotIn('precondition', headers['supported'][0])
        self.assertIn('audio', headers['contact'][0])

    def test_message_does_not_claim_sms_only_methods(self):
        _, headers, _ = imsd.parse(self.ims.request_msg('MESSAGE', 'sip:synthetic@example.invalid').decode())
        self.assertEqual(headers['allow'], [imsd.ALLOW])
        self.assertIn('INVITE', headers['allow'][0])

    def test_options_reports_audio_and_sdp_without_starting_media(self):
        self.daemon.on_sip(self.request(), self.peer, True)
        status, hdr, body = self.sent()
        self.assertEqual(status, 'SIP/2.0 200 OK')
        self.assertEqual(hdr['allow'], [imsd.ALLOW])
        self.assertIn('application/sdp', hdr['accept'][0])
        self.assertEqual(hdr['content-type'], ['application/sdp'])
        self.assertIn('m=audio 0 RTP/AVP', body)  # RFC3264 section9 capabilities
        self.assertIn('AMR-WB/16000', body)
        self.assertIn('AMR/8000', body)
        self.assertEqual(int(hdr['content-length'][0]), len(body.encode()))
        self.assertIsNone(self.daemon.calls.call)
        self.media.assert_not_called()
        self.ims.sc.sendto.assert_not_called()

    def test_options_declined_sdp_has_no_body(self):
        for accept in ('text/plain', 'application/sdp;q=0', 'application/sdp;q=0.0'):
            self.daemon.on_sip(self.request(extra=['Accept: ' + accept]), self.peer, True)
            _, hdr, body = self.sent()
            self.assertEqual(body, '')
            self.assertNotIn('content-type', hdr)

    def test_options_wildcards_and_nonzero_preference(self):
        for accept in ('*/*', 'application/*', 'application/sdp;q=0.5'):
            self.daemon.on_sip(self.request(extra=['Accept: ' + accept]), self.peer, True)
            self.assertIn('m=audio', self.sent()[2])

    def test_options_busy_and_in_dialog(self):
        self.daemon.calls.call = {'call_id': 'another-call'}
        self.daemon.on_sip(self.request(), self.peer, True)
        self.assertEqual(self.sent()[0], 'SIP/2.0 486 Busy Here')
        self.daemon.calls.call = {'call_id': 'private-test-id'}
        self.daemon.on_sip(self.request().replace(b'z9hG4bK-test', b'z9hG4bK-new'), self.peer, True)
        self.assertEqual(self.sent()[0], 'SIP/2.0 200 OK')

    def test_transaction_repetition_counted_not_logged(self):
        for _ in range(2):
            self.daemon.on_sip(self.request(), self.peer, True)
        self.assertEqual(self.daemon.sip_counts['request_OPTIONS'], 2)
        self.assertEqual(self.daemon.sip_counts['repeated_OPTIONS'], 1)
        text = repr(self.logs)
        for forbidden in ('private-test-id', 'synthetic@example', 'z9hG4bK-test'):
            self.assertNotIn(forbidden, text)

    def test_duplicate_message_replays_exact_response_without_redelivery(self):
        raw = self.request('MESSAGE', ['Content-Type: application/vnd.3gpp.sms'], b'synthetic-body')
        self.daemon.on_sip(raw, self.peer, True)
        first = self.ims.ss.sendto.call_args.args[0]
        self.daemon.on_sip(raw, self.peer, True)
        self.daemon.on_rp.assert_called_once()
        self.assertEqual(first, self.ims.ss.sendto.call_args.args[0])
        self.assertEqual(self.daemon.sip_counts['replayed_MESSAGE'], 1)

    def test_expired_reply_does_not_suppress_a_new_transaction(self):
        raw = self.request('MESSAGE', ['Content-Type: application/vnd.3gpp.sms'], b'synthetic-body')
        with patch('imsd.time.monotonic', return_value=100):
            self.daemon.on_sip(raw, self.peer, True)
        with patch('imsd.time.monotonic', return_value=165):
            self.daemon.on_sip(raw, self.peer, True)
        self.assertEqual(self.daemon.on_rp.call_count, 2)

    def test_different_transaction_is_delivered_normally(self):
        raw = self.request('MESSAGE', ['Content-Type: application/vnd.3gpp.sms'], b'synthetic-body')
        self.daemon.on_sip(raw, self.peer, True)
        self.daemon.on_sip(raw.replace(b'z9hG4bK-test', b'z9hG4bK-new'), self.peer, True)
        self.assertEqual(self.daemon.on_rp.call_count, 2)

    def test_rport_received_and_lower_via_order(self):
        raw = self.request(extra=['Via: SIP/2.0/UDP next.example.invalid;branch=next']).replace(
            b';branch=z9hG4bK-test', b';branch=z9hG4bK-test;rport')
        self.daemon.on_sip(raw, self.peer, True)
        _, hdr, _ = self.sent()
        self.assertIn(';rport=32001', hdr['via'][0])
        self.assertIn(';received=2001:db8::1', hdr['via'][0])
        self.assertEqual(hdr['via'][1], 'SIP/2.0/UDP next.example.invalid;branch=next')

    def test_response_diagnostic_omits_freeform_headers(self):
        self.daemon.on_sip(b'SIP/2.0 200 OK\r\nCSeq: 1 SUBSCRIBE\r\nFrom: private-identity\r\nContent-Length: 0\r\n\r\n', self.peer, True)
        self.assertEqual(self.daemon.sip_counts['response_SUBSCRIBE_200'], 1)
        self.assertNotIn('private-identity', repr(self.logs))

    def test_diagnostic_socket_exposes_only_counters_and_flags(self):
        self.daemon.on_client('local-client', b'{"op":"diagnostics"}')
        obj, client = self.daemon.emit.call_args.args
        self.assertEqual(obj, dict(op='diagnostics', registered=True, call_active=False, binding={}, sip_counts={}))
        self.assertEqual(client, 'local-client')

    def test_incoming_message_body_is_not_logged(self):
        body = b'private-message-body'
        self.daemon.on_sip(self.request('MESSAGE', ['Content-Type: application/vnd.3gpp.sms'], body), self.peer, True)
        self.daemon.on_rp.assert_called_once()
        self.assertNotIn(body.decode(), repr(self.logs))
        self.assertEqual(self.sent()[0], 'SIP/2.0 200 OK')

    def test_incoming_notify_xml_is_not_logged(self):
        body = b'<reginfo>private-subscriber</reginfo>'
        self.daemon.on_sip(self.request('NOTIFY', ['Event: reg'], body), self.peer, True)
        self.assertNotIn('private-subscriber', repr(self.logs))
        self.assertEqual(self.daemon.sip_counts['notify_reg'], 1)

    def test_required_preconditions_still_not_falsely_accepted(self):
        self.daemon.on_sip(self.request('INVITE', ['Require: precondition']), self.peer, True)
        self.assertEqual(self.sent()[0], 'SIP/2.0 420 Bad Extension')
        self.media.assert_not_called()

    def test_plain_incoming_invite_still_rings_without_starting_media(self):
        offer = self.daemon.calls.sdp(40000)
        self.daemon.on_sip(self.request('INVITE', ['Content-Type: application/sdp',
                                                 'Contact: <sip:synthetic@example.invalid>'], offer), self.peer, True)
        self.assertEqual(self.sent()[0], 'SIP/2.0 180 Ringing')
        self.assertEqual(self.daemon.calls.call['state'], 'ringing')
        self.media.assert_not_called()
        self.assertNotIn('private-test-id', repr(self.logs))

    def test_tcp_split_and_pipelined_frames(self):
        connection = SipConnection(Mock(), self.peer)
        first = self.request('OPTIONS')
        second = self.request('MESSAGE', ['Content-Type: application/vnd.3gpp.sms'], b'\x00\xff\r\n\r\n')
        self.assertEqual(connection.feed(first[:7]), [])
        self.assertEqual(connection.feed(first[7:] + second[:-3]), [first])
        self.assertEqual(connection.feed(second[-3:]), [second])
        self.assertEqual(connection.incoming, b'')

    def test_tcp_large_invite_is_not_limited_to_mtu(self):
        connection = SipConnection(Mock(), self.peer)
        raw = self.request('INVITE', ['Content-Type: application/sdp'], b'x' * 12000)
        self.assertEqual(connection.feed(raw), [raw])

    def test_tcp_invalid_or_ambiguous_lengths_rejected(self):
        for headers in ('', 'Content-Length: -1\r\n', 'Content-Length: 1\r\nl: 1\r\n',
                        'Content-Length: %d\r\n' % MAX_FRAME):
            connection = SipConnection(Mock(), self.peer)
            with self.assertRaises(ValueError):
                connection.feed(('OPTIONS sip:test SIP/2.0\r\n' + headers + '\r\n').encode())

    def test_tcp_partial_writes_queue_and_flush(self):
        sock = Mock()
        sent = []
        def first(data):
            sent.append(bytes(data[:3]))
            return 3
        sock.send.side_effect = [first, BlockingIOError()]  # use callable below for first write
        sock.send.side_effect = first
        connection = SipConnection(sock, self.peer)
        # Send the first3 bytes, then model a full kernel transmit buffer.
        calls = 0
        def limited(data):
            nonlocal calls
            calls += 1
            if calls == 1:
                return first(data)
            raise BlockingIOError()
        sock.send.side_effect = limited
        connection.send(b'abcdef')
        self.assertEqual(connection.outgoing, b'def')
        sock.send.side_effect = lambda data: sent.append(bytes(data)) or len(data)
        connection.flush()
        self.assertEqual(b''.join(sent), b'abcdef')
        self.assertEqual(connection.outgoing, b'')

    def test_tcp_response_stays_on_received_connection(self):
        sock = Mock()
        output = []
        sock.send.side_effect = lambda data: output.append(bytes(data)) or len(data)
        connection = SipConnection(sock, self.peer)
        raw = self.request().replace(b'SIP/2.0/UDP', b'SIP/2.0/TCP')
        self.daemon.on_sip(raw, connection.peer, True)
        self.assertTrue(b''.join(output).startswith(b'SIP/2.0 200 OK'))
        self.ims.ss.sendto.assert_not_called()
        self.assertEqual(self.daemon.sip_counts['request_transport_tcp'], 1)

    def test_tcp_duplicate_response_uses_same_stream(self):
        sock = Mock()
        output = []
        sock.send.side_effect = lambda data: output.append(bytes(data)) or len(data)
        connection = SipConnection(sock, self.peer)
        raw = self.request('MESSAGE', ['Content-Type: application/vnd.3gpp.sms'], b'synthetic')
        self.daemon.on_sip(raw, connection.peer, True)
        self.daemon.on_sip(raw, connection.peer, True)
        self.assertEqual(output[0], output[1])
        self.daemon.on_rp.assert_called_once()
        self.ims.ss.sendto.assert_not_called()

    def test_sa_stays_esp_with_exact_ports_and_both_transport_policies(self):
        rows = [('2001:db8::2', '2001:db8::1', 23000, 5063, 11, 'out'),
                ('2001:db8::1', '2001:db8::2', 5063, 23000, 12, 'in'),
                ('2001:db8::1', '2001:db8::2', 5064, 23001, 13, 'in'),
                ('2001:db8::2', '2001:db8::1', 23001, 5064, 14, 'out')]
        with patch('imsd.sh') as command:
            self.ims.install_sas(rows, '00' * 20)
        args = [call.args for call in command.call_args_list]
        states = [row for row in args if row[2] == 'state']
        policies = [row for row in args if row[2] == 'policy']
        self.assertEqual(len(states), 4)
        self.assertEqual(len(policies), 8)
        for n, state in enumerate(states):
            self.assertIn('auth-trunc', state)
            sel = state[state.index('sel'):]
            self.assertEqual(sel[sel.index('proto') + 1], 'any')
            self.assertEqual(sel[sel.index('sport') + 1], str(rows[n][2]))
            self.assertEqual(sel[sel.index('dport') + 1], str(rows[n][3]))
        for policy in policies:
            self.assertIn(policy[policy.index('proto') + 1], ('udp', 'tcp'))
            self.assertIn('esp', policy)


unittest.main()
