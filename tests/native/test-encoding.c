/* SPDX-License-Identifier: GPL-2.0-or-later
 * Exercise the actual patched LibVNCServer parser over a private socket pair.
 * This is NOT a RealVNC viewer qualification test. */
#include <assert.h>
#include <stdint.h>
#include <stdlib.h>
#include <sys/socket.h>
#include <unistd.h>
#include <rfb/rfb.h>

static void negotiate(rfbClientPtr client, int peer, const uint32_t *encodings,
                      unsigned count, int expected)
{
    uint8_t header[4] = {rfbSetEncodings, 0, 0, (uint8_t) count};
    assert(write(peer, header, sizeof header) == sizeof header);
    for (unsigned i = 0; i < count; ++i) {
        uint32_t wire = htonl(encodings[i]);
        assert(write(peer, &wire, sizeof wire) == sizeof wire);
    }
    rfbProcessClientMessage(client);
    assert(client->preferredEncoding == expected);
}

int main(int argc, char **argv)
{
    int sockets[2];
    uint32_t tight_zrle[] = {rfbEncodingTight, rfbEncodingZRLE};
    uint32_t tight[] = {rfbEncodingTight};
    uint32_t zrle_tight[] = {rfbEncodingZRLE, rfbEncodingTight};
    uint32_t unknown[] = {0x12345678};
    assert(socketpair(AF_UNIX, SOCK_STREAM, 0, sockets) == 0);
    rfbScreenInfoPtr screen = rfbGetScreen(&argc, argv, 64, 64, 8, 3, 4);
    assert(screen);
    screen->frameBuffer = calloc(64 * 64, 4);
    assert(screen->frameBuffer);
    rfbClientPtr client = rfbNewClient(screen, sockets[0]);
    assert(client);
    /* Handshake is outside this parser unit test; no network listener exists. */
    client->state = RFB_NORMAL;
    negotiate(client, sockets[1], tight_zrle, 2, rfbEncodingZRLE);
    negotiate(client, sockets[1], tight, 1, rfbEncodingRaw);
    negotiate(client, sockets[1], zrle_tight, 2, rfbEncodingZRLE);
    negotiate(client, sockets[1], unknown, 1, rfbEncodingRaw);
    /* ZRLE again first, so the empty list below proves the per-message reset and
     * not merely that Raw stayed Raw. */
    negotiate(client, sockets[1], zrle_tight, 2, rfbEncodingZRLE);
    negotiate(client, sockets[1], NULL, 0, rfbEncodingRaw);
    rfbCloseClient(client);
    rfbClientConnectionGone(client);
    close(sockets[1]);
    free(screen->frameBuffer);
    screen->frameBuffer = NULL;
    rfbScreenCleanup(screen);
    return 0;
}
