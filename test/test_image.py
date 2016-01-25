import os

import test_util
import mssapi
from mssapi.s3.connection import S3Connection
from mssapi.s3.key import Key

access_key = "68d3e9b0effa4974accc50ca04ec3bce" # need modify
secret_key = "b4a02742bfe74647a2bb1f1e8cbea75b" # need modify
port = 80
host = "msstest.vip.sankuai.com" # need modify
image_host = "msstest-img.sankuai.com" # need modify
image_port = 80

conn = S3Connection(
    aws_access_key_id = access_key,
    aws_secret_access_key = secret_key,
    port = port,
    host = host,
    image_host = image_host,
    image_port = image_port,
)

bucket_name = "image-sdk"
obj_name = "lena.jpg"

bucket = conn.get_bucket(bucket_name)
print "\n"

os.mkdir("pic")

obj_name = "lena.jpg"
process = "watermark=2&text=aGVsbG8sIHdvcmxk"
k0 = Key(bucket, obj_name, process, is_image=True)
k0.get_contents_to_filename("pic/lena_k0_water.jpg")
print k0.generate_url(400)

obj_name = "lena.jpg"
process = "30r"
k2 = bucket.get_image_key(obj_name, process)
k2.get_contents_to_filename("pic/lena_k2_30r.jpg")
print k2.generate_url(300)

files = os.listdir("pic")
test_util.assert_eq( 2, len(files), 'test delete_bucket' )
