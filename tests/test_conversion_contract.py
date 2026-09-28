import unittest

from pdf2md import parser


class ConversionContractTests(unittest.TestCase):
    def test_parser_exposes_a_stable_conversion_type(self):
        args = parser().parse_args(['--source-url', 'https://example.com/book.pdf'])

        self.assertEqual(args.conversion_type, 'pdf_to_md')

    def test_unknown_conversion_type_is_rejected(self):
        with self.assertRaises(SystemExit):
            parser().parse_args([
                '--source-url', 'https://example.com/book.pdf',
                '--conversion-type', 'image_to_text',
            ])
