#!/usr/bin/env python

from argparse import ArgumentParser, BooleanOptionalAction
from csv import writer
from fractions import Fraction
from io import BytesIO
from os import listdir
from os.path import isfile
from PIL import Image, UnidentifiedImageError
import pymupdf
from rectpack import newPacker

INITIAL_ROTATION_OPTIONS = {
    'none': (0, False),
    'counterclockwise': (90, True),
    'upside-down': (180, False),
    'clockwise': (-90, True),
}
INITIAL_ROTATION_OPTION_KEYS = tuple(INITIAL_ROTATION_OPTIONS.keys())
DEFAULT_INITIAL_ROTATION_OPTION_KEY = INITIAL_ROTATION_OPTION_KEYS[0]

PACKER_ROTATION_OPTIONS = {
    'none': None,
    'counterclockwise': 90,
    'clockwise': -90,
}
PACKER_ROTATION_OPTION_KEYS = tuple(PACKER_ROTATION_OPTIONS.keys())
DEFAULT_PACKER_ROTATION_OPTION_KEY = PACKER_ROTATION_OPTION_KEYS[0]

GRAVITY_OPTIONS = {
    'top-left': (False, False),
    'top-right': (True, False),
    'bottom-left': (False, True),
    'bottom-right': (True, True),
}
GRAVITY_OPTION_KEYS = tuple(GRAVITY_OPTIONS.keys())
DEFAULT_GRAVITY_OPTION_KEY = GRAVITY_OPTION_KEYS[0]

def none_coalesce(left, right):
    return right if left is None else left

# TODO: Make a "preview visualizer" using this
# https://dnmtechs.com/finding-the-average-color-of-an-image-in-python
def average_color(img):
    width, height = img.size
    pixels = img.load()
    r_total = 0
    g_total = 0
    b_total = 0
    for i in range(width):
        for j in range(height):
            r, g, b = pixels[i, j]
            r_total += r
            g_total += g
            b_total += b
    total_pixels = width * height
    avg_r = r_total // total_pixels
    avg_g = g_total // total_pixels
    avg_b = b_total // total_pixels
    return (avg_r, avg_g, avg_b)

def long_short_ratio(width_height, allow_rotation):
    width, height = width_height
    is_wide = width > height if allow_rotation else True
    long_side = width if is_wide else height
    short_side = height if is_wide else width
    return long_side, short_side, Fraction(long_side, short_side)

def fit_rect(bounds_long_short_ratio, rect_width_height, allow_rotation):
    bounds_long, bounds_short, bounds_ratio = bounds_long_short_ratio
    rect_long, rect_short, rect_ratio = long_short_ratio(rect_width_height, allow_rotation)
    if rect_long > bounds_long or rect_short > bounds_short:
        rect_width, rect_height = rect_width_height
        scaler = Fraction(bounds_long, rect_long) if rect_ratio > bounds_ratio else Fraction(bounds_short, rect_short)
        scaled_width = round(scaler * rect_width)
        scaled_height = round(scaler * rect_height)
        return (scaled_width, scaled_height), (rect_width * rect_height) - (scaled_width * scaled_height)
    else:
        return rect_width_height, 0

def format_efficiency(numerator, denominator):
    return('{}% ({}/{})'.format((numerator / denominator) * 100, numerator, denominator))

def parse_args():
    parser = ArgumentParser(conflict_handler='resolve')
    parser.add_argument('-a', '--allow-animated', action=BooleanOptionalAction,
                        help='Allow animated image files to be included, even though they won\'t be printed as such')
    parser.add_argument('-u', '--dpu', default=2400, type=float,
                        help='Dots Per Unit')     
    parser.add_argument('-p', '--dpp', default=8, type=float,
                        help='Dots Per Pixel (see https://en.wikipedia.org/wiki/Dots_per_inch#DPI_or_PPI_in_digital_image_files)')
    parser.add_argument('-w', '--page-width', default=8.5, type=float,
                        help='Page width in units')
    parser.add_argument('-h', '--page-height', default=11, type=float,
                        help='Page height in units')
    parser.add_argument('-e', '--page-padding', default=0, type=int,
                        help='Padding between images and page edges in pixels')
    parser.add_argument('--page-padding-top', type=int,
                        help='Padding between images and page top edge in pixels')
    parser.add_argument('--page-padding-right', type=int,
                        help='Padding between images and page right edge in pixels')
    parser.add_argument('--page-padding-bottom', type=int,
                        help='Padding between images and page bottom edge in pixels')
    parser.add_argument('--page-padding-left', type=int,
                        help='Padding between images and page left edge in pixels')
    parser.add_argument('-i', '--image-padding', default=0, type=int,
                        help='Padding between images in pixels')
    parser.add_argument('--initial-rotation', default=DEFAULT_INITIAL_ROTATION_OPTION_KEY, choices=INITIAL_ROTATION_OPTION_KEYS,
                        help='Direction to rotate images relative to page')
    parser.add_argument('-r', '--packer-rotation', default=DEFAULT_PACKER_ROTATION_OPTION_KEY, choices=PACKER_ROTATION_OPTION_KEYS,
                        help='Direction for packing algorithm to rotate images relative to the initial rotation, if it is allowed to do so')
    # Packer will produce the same results between interchanged initial and packer rotations, but square images will prioritize the former
    parser.add_argument('-g', '--gravity', default=DEFAULT_GRAVITY_OPTION_KEY, choices=GRAVITY_OPTION_KEYS,
                        help='Corner for images to gravitate towards')
    parser.add_argument('docname', help='Base output filename')
    parser.add_argument('input_files', nargs='+',
                        help='Filenames of images to pack')
    return parser.parse_args()
    # Shouldn't have to log anything already specified in the command
    # TODO: "flip padding, gravity, and rotation every other page" option

def main():
    args = parse_args()
    docname = args.docname
    image_contenders_filenames = args.input_files
    initial_rotation, initial_rotation_is_sideways = INITIAL_ROTATION_OPTIONS[args.initial_rotation]
    packer_rotation = PACKER_ROTATION_OPTIONS[args.packer_rotation]
    allow_packer_rotation = packer_rotation is not None
    gravitate_right, gravitate_down = GRAVITY_OPTIONS[args.gravity]
    no_animated_gifs = not args.allow_animated

    ppu = Fraction(args.dpu, args.dpp)
    page_width = int(ppu * args.page_width)
    page_height = int(ppu * args.page_height)

    page_padding = args.page_padding
    page_padding_top = none_coalesce(args.page_padding_top, page_padding)
    page_padding_right = none_coalesce(args.page_padding_right, page_padding)
    page_padding_bottom = none_coalesce(args.page_padding_bottom, page_padding)
    page_padding_left = none_coalesce(args.page_padding_left, page_padding)

    image_bounds_width = page_width - page_padding_right - page_padding_left
    image_bounds_height = page_height - page_padding_top - page_padding_bottom
    if image_bounds_width <= 0 or image_bounds_height <= 0:
        print('Error: image boundaries are non-positive')
        return 1
    image_bounds_long_short_ratio = long_short_ratio((image_bounds_width, image_bounds_height), allow_packer_rotation)

    image_padding = args.image_padding
    bin_width = image_bounds_width + image_padding
    bin_height = image_bounds_height + image_padding
    bin_area = bin_width * bin_height

    print('Measuring images...')
    images_filenames_and_orig_fitted_rect_dimensions = list()
    packer = newPacker(rotation = allow_packer_rotation)
    packer.add_bin(bin_width, bin_height, float('inf'))
    lost_pixels_total = 0
    rid = 0
    for image_contender_filename in image_contenders_filenames:
        if not isfile(image_contender_filename):
            continue
        try:
            image = Image.open(image_contender_filename)
        except UnidentifiedImageError:
            print('PIL didn\'t detect "{}" as an image, so it will not be added'.format(image_contender_filename))
            continue
        if no_animated_gifs and getattr(image, 'is_animated', False):
            print('"{}" is animated, so it will not be added'.format(image_contender_filename))
            continue
        orig_dimensions = image.size
        fitted_dimensions, lost_pixels = fit_rect(image_bounds_long_short_ratio, (orig_dimensions[1], orig_dimensions[0]) if initial_rotation_is_sideways else orig_dimensions, allow_packer_rotation)
        if lost_pixels > 0:
            print('"{}" will lose {} pixels from resizing to fit'.format(image_contender_filename, lost_pixels))
            lost_pixels_total += lost_pixels
        rect_dimensions = tuple(map(lambda dimension: dimension + image_padding, fitted_dimensions))
        images_filenames_and_orig_fitted_rect_dimensions.append((image_contender_filename, orig_dimensions, fitted_dimensions, rect_dimensions))
        packer.add_rect(*rect_dimensions, rid)
        rid += 1
    if lost_pixels_total > 0:
        print('Total pixels lost from resizing: {}'.format(lost_pixels_total))
    if rid == 0:
        print('Error: no images were found')
        return 1

    print('Packing images...')
    packer.pack()

    print('Adding images to PDF...')
    doc = pymupdf.open()
    total_rect_area = 0
    total_bin_area = 0
    with open('{}.csv'.format(docname), 'w') as location_log:
        location_logger = writer(location_log)
        columns = ('filename', 'orig_width', 'orig_height', 'page', 'x', 'y', 'width', 'height')
        if allow_packer_rotation:
            columns = (*columns, 'packer_rotated')
        location_logger.writerow(columns)
        # TODO: Allow splitting into multiple PDF files by page, either by a fixed count or a filesize limit
        for page_number, rect_bin in enumerate(packer, 1):
            bin_total_rect_area = 0
            page = doc.new_page(width=page_width, height=page_height)
            for rect in rect_bin:
                rect_width = rect.width
                rect_height = rect.height
                rect_area = rect_width * rect_height
                bin_total_rect_area += rect_area
                total_rect_area += rect_area

                filename, (orig_width, orig_height), (non_rotated_fitted_width, non_rotated_fitted_height), non_rotated_rect_dimensions = images_filenames_and_orig_fitted_rect_dimensions[rect.rid]
                x = page_width - page_padding_right - rect_width - rect.x if gravitate_right else page_padding_left + rect.x
                y = page_height - page_padding_bottom - rect_height - rect.y if gravitate_down else page_padding_top + rect.y
                rotate_bool = non_rotated_rect_dimensions != (rect_width, rect_height)
                rotate_degrees = initial_rotation + (packer_rotation if rotate_bool else 0)
                fitted_width, fitted_height = (non_rotated_fitted_height, non_rotated_fitted_width) if rotate_bool else (non_rotated_fitted_width, non_rotated_fitted_height)

                row = (filename, orig_width, orig_height, page_number, x, y, fitted_width, fitted_height)
                if allow_packer_rotation:
                    row = (*row, 'y' if rotate_bool else 'n')
                location_logger.writerow(row)
                pymupdf_rect = pymupdf.Rect(x, y, x + fitted_width, y + fitted_height)
                try:
                    page.insert_image(pymupdf_rect, rotate = rotate_degrees, stream = open(filename, 'rb').read())
                except pymupdf.mupdf.FzErrorFormat as e:
                    if str(e) == 'code=7: unknown image file format':
                        # https://pymupdf.readthedocs.io/en/latest/pixmap.html#supported-input-image-formats
                        # Handle any image that is not a BMP, JPEG, GIF, TIFF, JXR, JPX, PNG, PAM, PBM, PGM, PNM, or PPM, namely WEBPs
                        print('Converting "{}"...'.format(filename))
                        tmp_image_file = BytesIO()
                        Image.open(filename).save(tmp_image_file, 'png')
                        page.insert_image(pymupdf_rect, rotate = rotate_degrees, stream = tmp_image_file.getvalue())
                    else:
                        raise e
            print('Page {} efficiency: {}'.format(page_number, format_efficiency(bin_total_rect_area, bin_area)))
            total_bin_area += bin_area
    print('Overall efficiency: {}'.format(format_efficiency(total_rect_area, total_bin_area)))
    doc.save('{}.pdf'.format(docname))

    print('Done!')

if __name__ == '__main__':
    main()
