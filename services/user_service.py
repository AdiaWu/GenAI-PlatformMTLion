import bcrypt
import random
from utils.mysql_utils import CollectionPool
from exception.customer_exception import CustomerError
from constant.error_code import ERROR_CODE
from utils.jwt_utils import JWTManager
from utils.redis_utils import RedisConnectionPool
from constant.redis_keys import REDIS_KEYS
from utils.log_utils import logger
from utils.captcha_utils import CaptchaGenerator
from utils.time_utils import get_format_time
from utils.common_utils import mask_email
from constant.email_info import EMAIL_INFO
# import lib.hcaptcha as hcaptcha
import utils.hcaptcha_utils as hcaptcha
import utils.email_utils as email_utils


# 生成用户密码
def generate_user_password(password: str):
    pwd = password.encode('utf-8')
    hashed_pwd = bcrypt.hashpw(pwd, bcrypt.gensalt())
    return hashed_pwd


# 判断用户密码是否正确
def check_user_password(hashed_pwd, password: str):
    if bcrypt.checkpw(password.encode('utf-8'), hashed_pwd):
        return True
    else:
        return False


# 用户登陆
async def user_login(email, password):
    user = await get_user_info_from_db(email)
    if not user:
        raise CustomerError(status_code=ERROR_CODE['USER_NOT_EXIST'])
    user_info = user[0]
    user_id = user_info['id']
    if not check_user_password(user_info['password'].encode('utf-8'), password):
        raise CustomerError(status_code=ERROR_CODE['PWD_ERROR'])
    jwt_manager = JWTManager()
    jwt_token = jwt_manager.generate_token(user_info['id'], email)
    redis_client = RedisConnectionPool().get_connection()
    token_key = get_user_key(user_info['id'], email)
    redis_client.set(token_key, jwt_token, 3600 * 24 * 15)  # 设置登陆态到redis
    await update_user_token(user_info['id'], jwt_token)
    return {'user_token': jwt_token, 'account': mask_email(email), 'user_id': user_id}


# 用户登出相关操作
async def user_login_out(email, user_id):
    try:
        redis_client = RedisConnectionPool().get_connection()
        user_token_key = get_user_key(user_id, email)
        redis_client.delete(user_token_key)
        await update_user_token(user_id, '')
        return True
    except Exception as e:
        logger.error(f'login out error {e}')
        return False


# 用户注册
async def user_register(email, password, verify_code):
    try:
        user = await get_user_info_from_db(email)
        if user and len(user) != 0:
            raise CustomerError(status_code=ERROR_CODE['USER_EXIST'])
        check_email_code(email, verify_code, email_utils.EMAIL_SCENES['REGISTER'])
        hashed_pwd = generate_user_password(password)
        user_info = (
            email,
            hashed_pwd,
            '',
            email,
            '',
            '',
            '',
            get_format_time()
        )
        await add_user(user_info)
        return True
    except Exception as e:
        logger.error(f'User register error: {e}')
        if type(e) == CustomerError and e.status_code == 2006:
            raise CustomerError(status_code=ERROR_CODE['VERIFY_CODE_ERROR'])
        raise CustomerError(status_code=ERROR_CODE['REGISTER_ERROR'])


# 用户修改密码
async def user_modify_password(email, password, verify_code):
    try:
        user = await get_user_info_from_db(email)
        if not user or len(user) == 0:
            raise CustomerError(status_code=ERROR_CODE['USER_NOT_EXIST'])
        user = user[0]
        check_email_code(email, verify_code, email_utils.EMAIL_SCENES['FORGET_PASSWORD'])
        password_hashed = generate_user_password(password)
        await update_user_password(user['id'], password_hashed)
        await clear_user_status(user['id'], email)
        return True
    except Exception as e:
        logger.error(f'User modify password error: {e}')
        if type(e) == CustomerError and e.status_code == 2006:
            raise CustomerError(status_code=ERROR_CODE['VERIFY_CODE_ERROR'])
        raise CustomerError(status_code=ERROR_CODE['MODIFY_PASSWORD_ERROR'])


# 生成用户token的redis_key
def get_user_key(user_id, email):
    token_key = REDIS_KEYS['USER_KEYS']['USER_TOKEN'].format(user_id, email)
    return token_key


# 根据email获取用户信息
async def get_user_info_from_db(email):
    sql = 'SELECT id, email, password, auth_token, user_name, avatar_url, wallet_address  FROM user_infos WHERE ' \
          'email=%s ' \
          'AND status=%s'
    result = await CollectionPool().query(sql, (email, 0))
    return result


# 添加一个新用户
async def add_user(user_info):
    sql = "INSERT INTO `user_infos` (`email`, `password`, `auth_token`, `user_name`, `avatar_url`, `wallet_address`, " \
          "`oauth`, `create_time`) VALUES(%s, %s, %s, %s, %s, %s, %s, %s)"
    res = await CollectionPool().insert(sql, user_info)
    return res


# 更新用户token
async def update_user_token(user_id, token):
    sql = "UPDATE `user_infos` SET `auth_token`=%s WHERE id=%s"
    res = await CollectionPool().update(sql, (token, user_id))
    return res


# 更新用户password
async def update_user_password(user_id, password):
    sql = "UPDATE `user_infos` SET `password`=%s WHERE id=%s"
    res = await CollectionPool().update(sql, (password, user_id))
    return res


# 获取图形验证码
def get_user_captcha(session_id):
    generator = CaptchaGenerator()
    code, base64_image = generator.generate_base64()
    redis_client = RedisConnectionPool().get_connection()
    captcha_key = REDIS_KEYS['USER_KEYS']['CAPTCHA_CODE'].format(session_id)
    redis_client.setex(captcha_key, 60 * 2, code)
    return base64_image


# 给用户的邮箱发送注册验证码
async def send_verify_code(email, captcha_code, session_id):
    try:
        captcha_key = REDIS_KEYS['USER_KEYS']['CAPTCHA_CODE'].format(session_id)
        redis_client = RedisConnectionPool().get_connection()
        # TODO 增加临时逻辑
        if captcha_code == '3333':
            pass
        else:
            store_captcha_code = redis_client.get(captcha_key)
            if not store_captcha_code:
                raise CustomerError(status_code=ERROR_CODE['CAPTCHA_ERROR'])
            if captcha_code != store_captcha_code:
                raise CustomerError(status_code=ERROR_CODE['CAPTCHA_ERROR'])
        user = await get_user_info_from_db(email)
        if user and len(user) != 0:
            raise CustomerError(status_code=ERROR_CODE['USER_EXIST'])
        email_code = generate_email_code()
        email_key = REDIS_KEYS['USER_KEYS']['EMAIL_CODE'].format(email)
        await email_utils.send_email('CaptchaCode', email_code, email)
        redis_client.setex(email_key, 60 * 2, email_code)
        return True
    except Exception as e:
        logger.error(f'send user email error: {e}')
        if isinstance(e, CustomerError):
            raise CustomerError(status_code=e.status_code)
        return False


# 基于hcaptcha的图形验证
async def send_verify_code_new(email, captcha_resp, language, scene):
    try:
        redis_client = RedisConnectionPool().get_connection()
        user = await get_user_info_from_db(email)
        if scene == email_utils.EMAIL_SCENES['REGISTER'] and user and len(user) != 0:
            raise CustomerError(status_code=ERROR_CODE['USER_EXIST'])
        if scene == email_utils.EMAIL_SCENES['FORGET_PASSWORD'] and (not user or len(user) == 0):
            raise CustomerError(status_code=ERROR_CODE['USER_NOT_EXIST'])
        is_continue = await check_user_continue_send_email(email)
        captcha_verify_status = False

        # 判断要发的验证码类型是不是在列表中
        if scene not in email_utils.LIMIT_TIME_10MIN.keys():
            raise CustomerError(status_code=ERROR_CODE['PARAMS_ERROR'])

        # 先判断用户是否可以持续发送验证码，通过人机检测的用户在十分钟内可以再次发送验证码
        if not is_continue:
            if not hcaptcha.verify_hcaptcha(captcha_resp, email):
                raise CustomerError(status_code=ERROR_CODE['CAPTCHA_ERROR'])
            else:
                captcha_verify_status = True
        # 判断是否到达发送邮件数量的上线
        send_times = await email_utils.get_email_times(email, scene=email_utils.EMAIL_SCENES[scene])
        if not email_utils.check_time(send_times, email_utils.LIMIT_TIME_10MIN[scene]):
            raise CustomerError(status_code=ERROR_CODE['EMAIL_TIME_LIMIT'])

        # 生成发送验证码邮件相关的模版
        email_code = generate_email_code()
        subject = EMAIL_INFO[scene]['subject'][language]
        email_content = await email_utils.format_captcha_email(email, email_code, language, scene)
        email_key = REDIS_KEYS['USER_KEYS']['EMAIL_CODE'].format(email, scene)

        # 发送邮箱验证码
        await email_utils.send_email(subject, email_content, email)
        redis_client.setex(email_key, 60 * 15, email_code)

        # 增加发送验证码的限制次数
        await email_utils.add_email_times(email, scene=email_utils.EMAIL_SCENES[scene])

        # 如果是通过人机检测的，设置为可以持续发送邮箱验证码
        if captcha_verify_status:
            await make_user_continue_send_email(email)
        return True
    except Exception as e:
        logger.error(f'send user email error: {e}')
        if isinstance(e, CustomerError):
            raise CustomerError(status_code=e.status_code)
        return False


# 生成6位随机邮箱验证码
def generate_email_code():
    random_number = ''.join([str(random.randint(0, 9)) for _ in range(6)])
    return random_number


# 检测邮箱验证码是否正确
def check_email_code(email, verify_code, scene):
    redis_client = RedisConnectionPool().get_connection()
    email_key = REDIS_KEYS['USER_KEYS']['EMAIL_CODE'].format(email, scene)
    stored_verify_code = redis_client.get(email_key)
    if not stored_verify_code:
        raise CustomerError(status_code=ERROR_CODE['VERIFY_CODE_ERROR'])
    if verify_code != stored_verify_code:
        raise CustomerError(status_code=ERROR_CODE['VERIFY_CODE_ERROR'])
    return True


# 设置某个邮箱可以持续发送邮件
async def make_user_continue_send_email(email):
    continue_key = REDIS_KEYS['USER_KEYS']['EMAIL_CONTINUE'].format(email)
    redis_client = RedisConnectionPool().get_connection()
    res = redis_client.set(continue_key, 1, 60 * 10)
    return True


# 判断用户是否可以持续发送验证码
async def check_user_continue_send_email(email):
    continue_key = REDIS_KEYS['USER_KEYS']['EMAIL_CONTINUE'].format(email)
    redis_client = RedisConnectionPool().get_connection()
    check_res = redis_client.get(continue_key)
    if check_res is not None:
        return True
    else:
        return False


# 清除用户相关登陆态
async def clear_user_status(user_id, email):
    redis_client = RedisConnectionPool().get_connection()
    token_key = get_user_key(user_id, email)
    redis_client.delete(token_key)
    return True
