import os
import hashlib
import unittest

from scrapy.http import Request, Response
from scrapy.spider import Spider
from scrapy.utils.test import get_crawler
from scrapy.utils.python import to_bytes
from scrapy.exceptions import NotConfigured
from hubstorage import HubstorageClient

from scrapy_hcf import HcfMiddleware

HS_ENDPOINT = os.getenv('HS_ENDPOINT', 'http://localhost:8003')
HS_AUTH = os.getenv('HS_AUTH')


@unittest.skipUnless(HS_AUTH, 'No valid hubstorage credentials set')
class HcfTestCase(unittest.TestCase):

    hcf_cls = HcfMiddleware

    projectid = '2222222'
    spidername = 'hs-test-spider'
    frontier = 'test'
    slot = '0'
    number_of_slots = 1

    @classmethod
    def setUpClass(cls):
        cls.endpoint = HS_ENDPOINT
        cls.auth = HS_AUTH
        cls.hsclient = HubstorageClient(auth=cls.auth, endpoint=cls.endpoint)
        cls.project = cls.hsclient.get_project(cls.projectid)
        cls.fclient = cls.project.frontier

    @classmethod
    def tearDownClass(cls):
        cls.project.frontier.close()
        cls.hsclient.close()

    def setUp(self):
        class TestSpider(Spider):
            name = self.spidername
            start_urls = [
                'http://www.example.com/'
            ]

        self.spider = TestSpider()
        self.hcf_settings = {'HS_ENDPOINT': self.endpoint,
                             'HS_AUTH': self.auth,
                             'HS_PROJECTID': self.projectid,
                             'HS_FRONTIER': self.frontier,
                             'HS_CONSUME_FROM_SLOT': self.slot,
                             'HS_NUMBER_OF_SLOTS': self.number_of_slots}
        self._delete_slot()

    def tearDown(self):
        self._delete_slot()

    def _delete_slot(self):
        self.fclient.delete_slot(self.frontier, self.slot)

    def _build_response(self, url, meta=None):
        return Response(url, request=Request(url="http://www.example.com/parent.html", meta=meta))

    def _get_crawler(self, settings=None):
        crawler = get_crawler(settings_dict=settings)
        # simulate crawler engine
        class Engine():
            def __init__(self):
                self.requests = []
            def schedule(self, request, spider):
                self.requests.append(request)
        crawler.engine = Engine()

        return crawler

    def test_not_loaded(self):
        crawler = self._get_crawler({})
        self.assertRaises(NotConfigured, self.hcf_cls.from_crawler, crawler)

    def test_start_requests(self):
        crawler = self._get_crawler(self.hcf_settings)
        hcf = self.hcf_cls.from_crawler(crawler)

        # first time should be empty
        start_urls = self.spider.start_urls
        new_urls = list(hcf.process_start_requests(start_urls, self.spider))
        self.assertEqual(new_urls, ['http://www.example.com/'])

        # now try to store some URLs in the hcf and retrieve them
        fps = [{'fp': 'http://www.example.com/index.html'},
               {'fp': 'http://www.example.com/index2.html'}]
        self.fclient.add(self.frontier, self.slot, fps)
        self.fclient.flush()
        new_urls = [r.url for r in hcf.process_start_requests(start_urls, self.spider)]
        expected_urls = [r['fp'] for r in fps]
        self.assertEqual(new_urls, expected_urls)
        self.assertEqual(len(hcf.batch_ids), 1)

    def test_spider_output(self):
        crawler = self._get_crawler(self.hcf_settings)
        hcf = self.hcf_cls.from_crawler(crawler)

        # process new GET request
        response = self._build_response("http://www.example.com/qxg1231")
        request = Request(url="http://www.example.com/product/?qxp=12&qxg=1231", meta={'use_hcf': True})
        outputs = list(hcf.process_spider_output(response, [request], self.spider))
        self.assertEqual(outputs, [])
        expected_links = {'0': set(['http://www.example.com/product/?qxp=12&qxg=1231'])}
        self.assertEqual(dict(hcf.new_links), expected_links)

        # process new POST request (don't add it to the hcf)
        response = self._build_response("http://www.example.com/qxg456")
        request = Request(url="http://www.example.com/product/?qxp=456", method='POST')
        outputs = list(hcf.process_spider_output(response, [request], self.spider))
        self.assertEqual(outputs, [request])
        expected_links = {'0': set(['http://www.example.com/product/?qxp=12&qxg=1231'])}
        self.assertEqual(dict(hcf.new_links), expected_links)

        # process new GET request (without the use_hcf meta key)
        response = self._build_response("http://www.example.com/qxg1231")
        request = Request(url="http://www.example.com/product/?qxp=789")
        outputs = list(hcf.process_spider_output(response, [request], self.spider))
        self.assertEqual(outputs, [request])
        expected_links = {'0': set(['http://www.example.com/product/?qxp=12&qxg=1231'])}
        self.assertEqual(dict(hcf.new_links), expected_links)

        # Simulate close spider
        hcf.close_spider(self.spider, 'finished')

    def test_close_spider(self):
        crawler = self._get_crawler(self.hcf_settings)
        hcf = self.hcf_cls.from_crawler(crawler)

        # Save 2 batches in the HCF
        fps = [{'fp': 'http://www.example.com/index_%s.html' % i} for i in range(0, 200)]
        self.fclient.add(self.frontier, self.slot, fps)
        self.fclient.flush()

        # Read the first batch
        start_urls = self.spider.start_urls
        new_urls = [r.url for r in hcf.process_start_requests(start_urls, self.spider)]
        expected_urls = [r['fp'] for r in fps]
        self.assertEqual(new_urls, expected_urls)

        # Simulate extracting some new urls
        response = self._build_response("http://www.example.com/parent.html")
        new_fps = ["http://www.example.com/child_%s.html" % i for i in range(0, 50)]
        for fp in new_fps:
            request = Request(url=fp, meta={'use_hcf': True})
            list(hcf.process_spider_output(response, [request], self.spider))
        self.assertEqual(len(hcf.new_links[self.slot]), 50)

        # Simulate emptying the scheduler
        crawler.engine.requests = []

        # Simulate close spider
        hcf.close_spider(self.spider, 'finished')
        self.assertEqual(len(hcf.new_links[self.slot]), 0)
        self.assertEqual(len(hcf.batch_ids), 0)

        # HCF must be have 1 new batch
        batches = [b for b in self.fclient.read(self.frontier, self.slot)]
        self.assertEqual(len(batches), 1)

    def test_hcf_params(self):
        crawler = self._get_crawler(self.hcf_settings)
        hcf = self.hcf_cls.from_crawler(crawler)

        # Simulate extracting some new urls and adding them to the HCF
        response = self._build_response("http://www.example.com/parent.html")
        new_fps = ["http://www.example.com/child_%s.html" % i for i in range(0, 5)]
        new_requests = []
        for fp in new_fps:
            hcf_params = {'qdata': {'a': '1', 'b': '2', 'c': '3'},
                          'fdata': {'x': '1', 'y': '2', 'z': '3'},
                          'p': 1}
            request = Request(url=fp, meta={'use_hcf': True, "hcf_params": hcf_params})
            new_requests.append(request)
            list(hcf.process_spider_output(response, [request], self.spider))
        expected = set(['http://www.example.com/child_4.html',
                        'http://www.example.com/child_1.html',
                        'http://www.example.com/child_0.html',
                        'http://www.example.com/child_3.html',
                        'http://www.example.com/child_2.html'])
        self.assertEqual(hcf.new_links[self.slot], expected)

        # Simulate close spider
        hcf.close_spider(self.spider, 'finished')

        # Similate running another spider
        start_urls = self.spider.start_urls
        stored_requests = list(hcf.process_start_requests(start_urls, self.spider))
        for a, b in zip(new_requests, stored_requests):
            self.assertEqual(a.url, b.url)
            self.assertEqual(a.meta.get('qdata'), b.meta.get('qdata'))

        # Simulate emptying the scheduler
        crawler.engine.requests = []

        # Simulate close spider
        hcf.close_spider(self.spider, 'finished')

    def test_spider_output_override_slot(self):
        crawler = self._get_crawler(self.hcf_settings)
        hcf = self.hcf_cls.from_crawler(crawler)

        def get_slot_callback(request):
            md5 = hashlib.md5()
            md5.update(to_bytes(request.url))
            digest = md5.hexdigest()
            return str(int(digest, 16) % 5)
        self.spider.slot_callback = get_slot_callback

        # process new GET request
        response = self._build_response("http://www.example.com/qxg1231")
        request = Request(url="http://www.example.com/product/?qxp=12&qxg=1231",
                          meta={'use_hcf': True})
        outputs = list(hcf.process_spider_output(response, [request], self.spider))
        self.assertEqual(outputs, [])
        expected_links = {'4': set(['http://www.example.com/product/?qxp=12&qxg=1231'])}
        self.assertEqual(dict(hcf.new_links), expected_links)

        # Simulate close spider
        hcf.close_spider(self.spider, 'finished')

    def test_get_slot(self):
        crawler = self._get_crawler(self.hcf_settings)
        hcf = self.hcf_cls.from_crawler(crawler)
        hcf._get_slot(Request('http://foo.com/bar'))
