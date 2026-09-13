/* SPDX-License-Identifier: GPL-2.0-or-later */
#ifndef WAYLAND_VNC_BUFFER_LAYOUT_H
#define WAYLAND_VNC_BUFFER_LAYOUT_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Reject wrapped chunks and negative stride; only packed positive-stride frames.
 * mapoffset locates the spa_data region; chunk_offset is relative to that region.
 * Validate the backing fd length separately before mmap/copy. */
static inline bool
wvnc_buffer_layout(uint32_t width, uint32_t height, uint32_t bpp,
                   int32_t stride, uint32_t maxsize, uint32_t mapoffset,
                   uint32_t chunk_offset, uint32_t chunk_size,
                   size_t *mapping_size, size_t *pixel_offset)
{
    size_t row_bytes, span;
    if (!width || !height || !bpp || stride <= 0 || width > SIZE_MAX / bpp)
        return false;
    row_bytes = (size_t) width * bpp;
    if (row_bytes > (size_t) stride ||
        (size_t) (height - 1) > (SIZE_MAX - row_bytes) / (size_t) stride)
        return false;
    span = (size_t) (height - 1) * (size_t) stride + row_bytes;
    if (chunk_offset > maxsize || chunk_size > maxsize - chunk_offset ||
        span > chunk_size)
        return false;
    *mapping_size = (size_t) mapoffset + maxsize;
    if (*mapping_size < maxsize)
        return false;
    *pixel_offset = (size_t) mapoffset + chunk_offset;
    return true;
}
#endif
