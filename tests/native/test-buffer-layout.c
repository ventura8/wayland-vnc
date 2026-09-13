/* SPDX-License-Identifier: GPL-2.0-or-later */
#include <assert.h>
#include "buffer-layout.h"

int main(void)
{
    size_t length = 0, offset = 0;
    assert(wvnc_buffer_layout(2, 2, 4, 8, 16, 0, 0, 16, &length, &offset));
    assert(length == 16 && offset == 0);
    assert(wvnc_buffer_layout(2, 2, 4, 16, 32, 4096, 8, 24, &length, &offset));
    assert(length == 4128 && offset == 4104);
    assert(!wvnc_buffer_layout(2, 2, 4, -8, 16, 0, 0, 16, &length, &offset));
    assert(!wvnc_buffer_layout(2, 2, 4, 7, 16, 0, 0, 16, &length, &offset));
    assert(!wvnc_buffer_layout(0, 2, 4, 8, 16, 0, 0, 16, &length, &offset));
    assert(!wvnc_buffer_layout(2, 0, 4, 8, 16, 0, 0, 16, &length, &offset));
    assert(!wvnc_buffer_layout(2, 2, 0, 8, 16, 0, 0, 16, &length, &offset));
    assert(!wvnc_buffer_layout(2, 2, 4, 8, 16, 0, 1, 16, &length, &offset));
    assert(!wvnc_buffer_layout(2, 2, 4, 8, 16, 0, 17, 0, &length, &offset));
    assert(!wvnc_buffer_layout(2, 2, 4, 8, 16, 0, 0, 15, &length, &offset));
    assert(!wvnc_buffer_layout(UINT32_MAX, UINT32_MAX, 4, INT32_MAX,
                               UINT32_MAX, 0, 0, UINT32_MAX, &length, &offset));
    return 0;
}
